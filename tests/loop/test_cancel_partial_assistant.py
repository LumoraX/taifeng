"""取消时 partial assistant 文本落史（ADR 0029 / R5）：UI 已看到的文本 transcript 里也要有。

自定义 client：流出 3 个 TextDelta 后停在一个永不点亮的 Event 上，给 CancelTurn 留窗口。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.client import ModelClient
from taifeng.llm.events import created, server_model, text_delta
from taifeng.loop.submission import CancelTurn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


class _StallingSession:
    """3 个 delta 后停住（尊重 token：token 取消时抛 CancelledError）。"""

    def __init__(self, cancel: CancellationToken, stalled: asyncio.Event) -> None:
        self._cancel = cancel
        self._stalled = stalled

    async def __aenter__(self) -> _StallingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        yield created()
        yield server_model("mock-model")
        for piece in ("第一段。", "第二段。", "第三段。"):
            yield text_delta(piece)
        self._stalled.set()
        await self._cancel.wait_cancelled()
        self._cancel.raise_if_cancelled()


class _StallingClient(ModelClient):
    def __init__(self) -> None:
        self.stalled = asyncio.Event()

    def session(
        self, *, cancel: CancellationToken, model: str | None = None,
    ) -> _StallingSession:
        return _StallingSession(cancel, self.stalled)


async def test_cancelled_turn_persists_partial_assistant_text(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = _StallingClient()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        sub_id = await engine.submit(taifeng.UserMessage(text="hi"))
        await asyncio.wait_for(client.stalled.wait(), timeout=3.0)
        await engine.submit(CancelTurn(submission_id=sub_id))

        async def _terminal() -> dict[str, Any]:
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    return dict(ev.msg.data)
            return {}

        data = await asyncio.wait_for(_terminal(), timeout=3.0)
        assert data.get("end_reason") == "cancelled"
        await asyncio.sleep(0.1)

        cold = [it async for it in await pool.store.load_thread(engine.thread_id)]
        assert cold[-1].kind == "assistant_message"
        assert cold[-1].payload["text"] == "第一段。第二段。第三段。"
        assert cold[-1].metadata.get("truncated") is True
        hot = engine.history_snapshot()
        assert [it.id for it in hot] == [it.id for it in cold]
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# resume 执行已批准工具的 token 必须派生自 engine 根 token（R4）
# ---------------------------------------------------------------------------


async def test_engine_shutdown_cancels_tool_running_under_resume(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """挂起 → Resume(granted) → 工具在 resume 路径执行中阻塞 → shutdown 必须能叫停它。

    此前 _execute_resumed_tool 用全新 CancellationToken()，与 _root_cancel 无父子
    关系，shutdown / pool close 的级联取消无法中止该工具。
    """
    from taifeng.llm.providers import SimClient, SimTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.submission import Resume, Shutdown
    from taifeng.permission.types import PermissionPolicy, PermissionRequest, SuspendingPrompter
    from taifeng.suspend.record import SuspensionRecord
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
    from tests.test_suspend import _build_suspend_skill

    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        req = PermissionRequest.for_tool_call(
            "danger", args, thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("root",), extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        # resume 二次放行后：阻塞直到被取消（模拟长工具）。converge 的 task.cancel
        # 可能先于 token 唤醒到达，所以在 finally 里看 token 状态——R4 契约是
        # 「工具能从 ctx.cancel 观察到 engine 级取消」
        tool_started.set()
        try:
            await ctx.cancel.wait_cancelled()
        finally:
            if ctx.cancel.is_cancelled:
                tool_cancelled.set()
        return ToolResult.error("cancelled", reason="cancelled")

    gated = ToolSpec(
        name="danger", description="gated", handler=handler, parallel_safe=True,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    _build_suspend_skill(skills_dir)
    client = SimClient(turns=[
        SimTurn(text="calling danger",
                tool_calls=[{"id": "call_d1", "name": "danger", "arguments": "{}"}],
                usage=TokenUsage(input_tokens=10, output_tokens=5)),
        SimTurn(text="never", usage=TokenUsage(input_tokens=1, output_tokens=1)),
    ])
    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[gated], permission_policy=policy,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="suspend-skill")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))

    async def _until(kinds: tuple[str, ...], sid: str) -> None:
        async for ev in engine.subscribe(sid):
            if ev.msg.kind in kinds:
                return

    await asyncio.wait_for(_until(("turn_suspended",), sub_id), timeout=3.0)
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    rec = SuspensionRecord.from_item(next(it for it in items if it.kind == "suspension"))
    req_id = rec.pending[0].request_id

    await engine.submit(Resume(thread_id=engine.thread_id, resolutions={req_id: {"granted": True}}))
    await asyncio.wait_for(tool_started.wait(), timeout=3.0)

    await engine.submit(Shutdown())
    await asyncio.wait_for(tool_cancelled.wait(), timeout=3.0)
    await pool.close()
