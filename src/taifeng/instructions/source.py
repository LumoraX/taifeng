"""InstructionSource 协议 + InstructionFetchError 异常。

设计参见 ``docs/architecture/capabilities/instructions-injection.md``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng.llm.errors import LLMError

if TYPE_CHECKING:
    from taifeng.instructions.types import InstructionContext


@runtime_checkable
class InstructionSource(Protocol):
    """业务侧实现 —— 返回当前 ctx 对应的指令文本。

    返回 ``None`` 表示"本次不注入"（与抛异常区分）。

    实现 SHALL 协程并发安全（多个 turn 可能并行调用）。
    实现 SHALL NOT 自行发起 HITL 询问机制 ——
    HITL 由 ``PermissionPolicy + PermissionPrompter`` 统一承担；
    业务侧权限不足 / 配额超限应直接 raise 或返回 None。
    """

    async def fetch(self, ctx: InstructionContext) -> str | None: ...


class InstructionFetchError(LLMError):
    """InstructionSource.fetch 抛任何异常时被包成此类。

    - turn SHALL 失败（不允许 silent fallback 到空字符串）
    - 发 EventMsg ``instruction_fetch_failed``
    - 取消触发时 SHALL 抛 ``anyio.get_cancelled_exc_class()`` 而非本异常
    """

    kind = "instruction_fetch_failed"

    def __init__(self, layer_name: str, cause: Exception) -> None:
        super().__init__(
            f"InstructionSource fetch failed for layer={layer_name!r}: {cause}"
        )
        self.layer_name = layer_name
        self.cause = cause
