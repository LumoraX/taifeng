"""Script 运行时数据类型 —— ``ScriptDescriptor`` / ``ScriptInvocation`` / ``ScriptResult``。

设计要点（参见 ``docs/architecture/capabilities/script-execution.md``）：

- ``ScriptDescriptor`` 是 loader 解析 SKILL.md 后产出的不可变元数据；executor 不修改它
- ``ScriptInvocation`` 把单次调用的参数 + 取消 token 绑定到 descriptor
- ``ScriptResult`` 把 subprocess 的物理结果落地，失败不抛异常（错误体现在 ``exit_code``
  与 ``is_timeout`` / ``killed`` 标志位上）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from taifeng.loop.cancellation import CancellationToken

# === 默认值 ===
DEFAULT_TIMEOUT_SECONDS: float = 60.0
DEFAULT_MAX_OUTPUT_BYTES: int = 16 * 1024  # 16 KiB（stdout + stderr 各自上限）

ScriptLanguage = Literal["shell", "python", "custom"]


@dataclass(frozen=True)
class ScriptDescriptor:
    """脚本元数据。由 SKILL.md frontmatter 或目录扫描产出。

    Attributes:
        skill_id: 所属 skill 的 ID（用于权限 target、日志、telemetry）
        name: 脚本逻辑名（同一 skill 内唯一）
        path: 脚本绝对路径。loader SHALL 校验其落在 skill 目录下
        language: 执行器选择键 —— shell / python / custom
        description: 给 LLM 看的一句话描述（决定要不要调用）
        args_schema: JSON Schema 形式的参数约束。argv 展开顺序按
            ``properties`` 的字段插入顺序，``required`` 列出必填项
        timeout_seconds: 单次执行硬超时；超过 SHALL SIGTERM → 1s grace → SIGKILL
        max_output_bytes: stdout / stderr 各自的字节上限；超过则截断 + ``truncated=True``
    """

    skill_id: str
    name: str
    path: Path
    language: ScriptLanguage
    description: str = ""
    args_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        """声明期自校验 —— 防止 loader 漏判落到运行时才炸。"""
        if not self.name:
            raise ValueError(f"ScriptDescriptor.name 不能为空 (skill={self.skill_id!r})")
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"ScriptDescriptor.timeout_seconds 必须 > 0，得到 {self.timeout_seconds}"
            )
        if self.max_output_bytes <= 0:
            raise ValueError(
                f"ScriptDescriptor.max_output_bytes 必须 > 0，得到 {self.max_output_bytes}"
            )

    @property
    def full_target(self) -> str:
        """权限 / telemetry 使用的归一化 target 标识。"""
        return f"{self.skill_id}/{self.name}"


@dataclass(frozen=True)
class ScriptInvocation:
    """单次脚本调用。

    Attributes:
        descriptor: 元数据；不可变
        args: 已校验通过 ``args_schema`` 的参数字典
        cancel: 取消 token（父 turn 的 cancel 派生）；executor 在 await 点 SHALL 检查
    """

    descriptor: ScriptDescriptor
    args: dict[str, Any]
    cancel: CancellationToken


@dataclass(frozen=True)
class ScriptResult:
    """脚本执行结果。

    Attributes:
        exit_code: 子进程退出码；SIGTERM/SIGKILL 时分别为 ``-15`` / ``-9``（POSIX 约定）
        stdout: 截断后的 stdout（UTF-8 已 lossy 解码）
        stderr: 截断后的 stderr
        duration_ms: 从 spawn 到收尾的耗时
        truncated: 是否触发输出截断
        is_timeout: 是否因超时被 kill
        killed: 是否因取消 / 超时被强制 kill（与 ``is_timeout`` 互不蕴含；取消单独算）
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    is_timeout: bool = False
    killed: bool = False

    @property
    def ok(self) -> bool:
        """退出码 0 且未被超时 / kill 视为成功。"""
        return self.exit_code == 0 and not self.is_timeout and not self.killed
