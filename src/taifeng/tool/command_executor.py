"""命令执行器协议 —— shell 类工具派生子进程的唯一 seam（sandbox-seam）。

``shell_exec`` 与 ``run_in_background`` 此前直接 ``asyncio.create_subprocess_*``，宿主想把
命令放进 Docker / firejail / 远端沙箱只能整个重写工具。现在两者都经 ``CommandExecutor``
启动进程：内核只定协议 + 本机默认实现，隔离实现由宿主注入（ADR 0017 规则③；与
``ScriptExecutor`` 同一范式，参照 deepagents 的 sandbox backend / codex ``sandboxing``）。

协议只管「启动」：返回的进程对象提供 ``communicate`` / ``kill`` / ``wait`` / ``returncode``——
``asyncio.subprocess.Process`` 天然满足，远端实现包一层即可。超时、取消、输出截断、
权限审批仍由工具统一负责，保证换执行器不改变这些语义。
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CommandSpec:
    """一次命令执行请求。

    Attributes:
        command: 命令文本。
        shell: True → 交给 shell 解释（管道 / 重定向可用）；False → ``shlex.split`` 后直接 exec。
        cwd: 工作目录（None = 执行器默认）。
        env: 完整环境变量（工具已按白名单构造，执行器不得再合并宿主环境）。
    """

    command: str
    shell: bool
    cwd: str | None
    env: dict[str, str]


@runtime_checkable
class CommandProcess(Protocol):
    """已启动进程的最小接口（``asyncio.subprocess.Process`` 天然满足）。"""

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        ...

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        ...

    def kill(self) -> None:
        """强制终止。"""
        ...

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        ...


@runtime_checkable
class CommandExecutor(Protocol):
    """命令执行器：按 ``CommandSpec`` 启动进程。

    实现方约束：
        - SHALL 使用 ``spec.env`` 作为完整环境（不叠加宿主 ``os.environ``，防凭据泄漏）；
        - SHALL 让 stdout / stderr 可经 ``communicate`` 读取；
        - 启动失败 SHALL 抛 ``OSError``（工具据此返回 ``spawn_error``）。
    """

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """启动进程并立即返回（不等待结束）。"""
        ...


class LocalCommandExecutor:
    """默认执行器：本机子进程（与此前工具内联的行为逐字一致）。"""

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """``shell=True`` 走 ``create_subprocess_shell``，否则 ``shlex.split`` 后 exec。"""
        if spec.shell:
            return await asyncio.create_subprocess_shell(
                spec.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=spec.env,
            )
        return await asyncio.create_subprocess_exec(
            *shlex.split(spec.command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=spec.env,
        )


__all__ = ["CommandExecutor", "CommandProcess", "CommandSpec", "LocalCommandExecutor"]
