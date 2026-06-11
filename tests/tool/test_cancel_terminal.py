"""K5：取消终态守卫 —— token 取消优雅终结、外部 task.cancel 传播、恰好一次。"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


def _runtime_with(handler) -> ToolCallRuntime:  # noqa: ANN001
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="t", description="x", input_schema={"type": "object"},
        handler=handler, parallel_safe=True,
    ))
    return ToolCallRuntime(reg)


async def test_token_cancel_finalizes_as_cancelled_result() -> None:
    """token 取消 → 终结为 cancelled ToolResult（恰好一次，供配对 output）。"""
    async def _h(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("should-not-reach")

    rt = _runtime_with(_h)
    tok = CancellationToken(name="t")
    tok.cancel()  # 先取消
    result = await rt.dispatch(
        name="t", arguments={}, ctx=ToolContext(call_id="c", cancel=tok, thread_id="t"),
    )
    assert result.is_error
    assert "cancelled" in result.output


async def test_foreign_cancel_propagates_not_swallowed() -> None:
    """非 token 的 asyncio.CancelledError（外部 task.cancel）→ 不吞、向上传播。"""
    async def _h(args: dict, ctx: ToolContext) -> ToolResult:
        raise asyncio.CancelledError("external")

    rt = _runtime_with(_h)
    tok = CancellationToken(name="t")  # token 未取消
    with pytest.raises(asyncio.CancelledError):
        await rt.dispatch(
            name="t", arguments={},
            ctx=ToolContext(call_id="c", cancel=tok, thread_id="t"),
        )


async def test_dispatch_batch_exactly_one_outcome_per_call_under_cancel() -> None:
    """并发批在 token 取消下：每个 request 恰好一个 outcome（无重复/无丢失）。"""
    async def _h(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("ok")

    rt = _runtime_with(_h)
    parent = CancellationToken(name="turn")
    parent.cancel()  # 整批在已取消状态下派发

    def ctx_for(call_id: str) -> ToolContext:
        return ToolContext(
            call_id=call_id, cancel=parent.child(f"tool:{call_id}"), thread_id="t",
        )

    async def emit(_msg: object) -> None:
        return None

    reqs = [
        ToolCallRequest(
            index=i, call_id=f"c{i}", name="t", arguments={},
            arguments_raw="{}", parallel_safe=True,
        )
        for i in range(4)
    ]
    outcomes = await dispatch_batch(
        reqs, runtime=rt, ctx_for=ctx_for, hooks=None, emit=emit,
        semaphore=asyncio.Semaphore(4), thread_id="t",
        submission_id="s", entry_skill_id="e",
        visible_tools=frozenset({"t"}),
    )
    # 恰好一次：4 个 request → 4 个 outcome，index 升序，各自 cancelled
    assert [o.index for o in outcomes] == [0, 1, 2, 3]
    assert all(o.result.is_error and "cancelled" in o.result.output for o in outcomes)
