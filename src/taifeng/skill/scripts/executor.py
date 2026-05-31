"""``ScriptExecutor`` 协议 + ``ScriptExecutionError`` 异常。

业务侧通过实现 ``ScriptExecutor`` 协议自定义执行环境（容器 / 沙箱 / 远程 RPC），
src 内不假设宿主进程具备执行能力（参见 ADR 0009）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from taifeng.llm.errors import LLMError
from taifeng.skill.scripts.types import ScriptDescriptor, ScriptInvocation, ScriptResult


class ScriptExecutionError(LLMError):
    """脚本执行过程中的不可恢复异常。

    注意：``exit_code != 0`` / 超时 / 截断属于 **正常结果** 走 ``ScriptResult`` 返回，
    SHALL NOT 抛 ``ScriptExecutionError``。本异常仅用于：
        - 找不到可执行文件 / 权限不足等系统级失败
        - executor 自身初始化失败（如 docker daemon 不可达）
    """

    kind = "script_execution_failed"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        descriptor: ScriptDescriptor,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.descriptor = descriptor
        self.cause = cause


@runtime_checkable
class ScriptExecutor(Protocol):
    """脚本执行器协议。业务侧实现以替换默认 subprocess 行为。

    实现方约束：
        - SHALL 异步：``execute`` 必须是 ``async def``，内部 IO 用 ``anyio`` 风格
        - SHALL 接受 cancel：``inv.cancel`` 触发后尽快返回（结果设 ``killed=True``）
        - SHALL 强制 timeout：超过 ``inv.descriptor.timeout_seconds`` 必须 kill 子任务
        - SHALL NOT 抛业务异常：正常退出码 / 超时 / 取消都通过 ``ScriptResult`` 返回；
          仅在系统级失败时抛 ``ScriptExecutionError``
        - SHALL 限制输出：超过 ``max_output_bytes`` 截断 + 设 ``truncated=True``
    """

    async def execute(self, inv: ScriptInvocation) -> ScriptResult:
        """执行单次调用，返回物理结果。"""
        ...
