"""P0 压缩鲁棒性：G1c 降级告警 / G1b 配对完整性回滚 / G2b 发送前预检。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.conversation.models import (
    function_call,
    function_call_output,
    user_message,
)
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg
from taifeng.loop.turn import TurnRunner, _history_orphan_call_ids
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _FakeStore:
    """最小内存 store —— 仅满足 run() / _maybe_compress 的 append 调用。"""

    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, item: object) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


class _NoopCompactStrategy:
    """成功压缩但不改 history（不引入孤儿）—— 用于触发计数 / 降级告警。"""

    name = "noop"
    priority = 100

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger:
        return CompressionTrigger(reason="user_request", threshold_pct=0.0)

    async def compress(
        self, ctx: CompressionContext, injection: InitialContextInjection
    ) -> CompressionResult:
        return CompressionResult(
            success=True,
            cache_invalidated=False,
            anchor_preserved_until=ctx.cache_anchor_index,
            new_history=list(ctx.history),
            removed_item_count=0,
        )


class _OrphaningStrategy:
    """成功压缩但删掉某 function_call 却保留其 output → 制造配对孤儿。"""

    name = "orphan"
    priority = 100

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger:
        return CompressionTrigger(reason="user_request", threshold_pct=0.0)

    async def compress(
        self, ctx: CompressionContext, injection: InitialContextInjection
    ) -> CompressionResult:
        new = [
            it
            for it in ctx.history
            if not (
                it.kind == "function_call"
                and it.payload.get("call_id") == "orphan"
            )
        ]
        return CompressionResult(
            success=True,
            cache_invalidated=False,
            anchor_preserved_until=-1,
            new_history=new,
            removed_item_count=1,
        )


async def _make_runner(
    skills_dir: Path,
    *,
    compressors: CompressionOrchestrator | None = None,
    budget: ContextBudget | None = None,
    history: list | None = None,
    events: list | None = None,
    **extra: object,
) -> TurnRunner:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: EventMsg) -> None:
        if events is not None:
            events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=MockClient(turns=[MockTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=compressors,
        dispatch_policy=DispatchPolicy(),
        budget=budget or ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=history if history is not None else [],
        **extra,
    )


def test_orphan_detection_symmetric_difference() -> None:
    """未配对的 call_id 被识别为孤儿；成对的不算。"""
    paired = [
        function_call("c1", "x", "{}", thread_id="t"),
        function_call_output("c1", "o", thread_id="t"),
    ]
    assert _history_orphan_call_ids(paired) == set()
    orphan = [function_call_output("lonely", "o", thread_id="t")]
    assert _history_orphan_call_ids(orphan) == {"lonely"}


@pytest.mark.asyncio
async def test_g1b_rolls_back_when_compaction_creates_orphan(
    skills_dir: Path,
) -> None:
    """压缩引入新孤儿 → 不应用、保留原 history、emit 完整性回滚。"""
    history = [
        user_message("hi", thread_id="t"),
        function_call("orphan", "tool_x", "{}", thread_id="t"),
        function_call_output("orphan", "out", thread_id="t"),
        user_message("more " + "x" * 80, thread_id="t"),
        user_message("tail " + "y" * 80, thread_id="t"),
    ]
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_OrphaningStrategy()]),
        history=list(history),
        events=events,
    )
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    kinds = [m.kind for m in events]
    assert "compaction_integrity_rolled_back" in kinds
    # history 未被改动（仍含被删的 function_call）
    assert runner.history_buffer == history
    # 计数不增加（未真正压缩）
    assert runner.compaction_count == 0


@pytest.mark.asyncio
async def test_g1c_degradation_warning_after_threshold(skills_dir: Path) -> None:
    """成功压缩计数达阈值后，每次压缩 emit 降级告警。"""
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_NoopCompactStrategy()]),
        history=[user_message(f"m{i} " + "x" * 40, thread_id="t") for i in range(6)],
        events=events,
        compaction_degradation_threshold=2,
    )
    for _ in range(3):
        await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    assert runner.compaction_count == 3
    warns = [m for m in events if m.kind == "compaction_degradation_warning"]
    # 第 2、3 次压缩各 emit 一次（count>=2）
    assert len(warns) == 2
    assert warns[0].data["compaction_count"] == 2
    assert warns[0].data["threshold"] == 2


@pytest.mark.asyncio
async def test_g2b_preflight_emits_when_over_hard_limit(skills_dir: Path) -> None:
    """发送前估算 token 超 hard limit → emit ContextBudgetExceeded（非阻塞）。"""
    events: list = []
    tiny_budget = ContextBudget(
        context_window=100, soft_limit_ratio=0.4, hard_limit_ratio=0.5
    )
    runner = await _make_runner(
        skills_dir,
        budget=tiny_budget,
        # 远超 hard_limit(=50 token) 的历史
        history=[user_message("z" * 4000, thread_id="t")],
        events=events,
    )
    await runner.run()

    kinds = [m.kind for m in events]
    assert "context_budget_exceeded" in kinds
    ev = next(m for m in events if m.kind == "context_budget_exceeded")
    assert ev.data["hard_limit"] == tiny_budget.hard_limit
    assert ev.data["token_estimate"] > tiny_budget.hard_limit


@pytest.mark.asyncio
async def test_g2b_body_size_guard_fails_before_send(skills_dir: Path) -> None:
    """max_request_bytes 启用且请求体超限 → 发送前抛 RequestTooLargeError，
    turn 以 failure_class=request_size 失败（未触达 LLM）。"""
    events: list = []
    budget = ContextBudget(context_window=1_000_000, max_request_bytes=100)
    runner = await _make_runner(
        skills_dir,
        budget=budget,
        history=[user_message("z" * 5000, thread_id="t")],
        events=events,
    )
    await runner.run()

    failed = next(m for m in events if m.kind == "turn_failed")
    assert failed.data["failure_class"] == "request_size"
    assert failed.data["kind"] == "RequestTooLargeError"
