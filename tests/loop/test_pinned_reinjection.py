"""postcompact-state-reinjection 接线测试:压缩成功后 pinned 状态钉回 tail。

覆盖:基本重注入(尾部 system_injection + 持久化 + 事件)/ 总预算丢弃 /
渲染 None 与异常 / 压缩失败不注入 / anchor 不受影响。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.pinned_state import PinnedStateRegistry
from taifeng.conversation.models import user_message
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.context.injection import InitialContextInjection
    from taifeng.loop.event import EventMsg


class _FakeStore:
    """最小内存 store —— 记录 append 以断言持久化(R5)。"""

    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, item: object) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


class _NoopCompactStrategy:
    """成功压缩但不改 history —— 隔离验证重注入步骤本身。"""

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


class _FailingStrategy:
    """触发但压缩失败 —— 失败路径不应重注入。"""

    name = "fail"
    priority = 100

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger:
        return CompressionTrigger(reason="user_request", threshold_pct=0.0)

    async def compress(
        self, ctx: CompressionContext, injection: InitialContextInjection
    ) -> CompressionResult:
        return CompressionResult(
            success=False,
            cache_invalidated=False,
            anchor_preserved_until=ctx.cache_anchor_index,
            new_history=list(ctx.history),
            removed_item_count=0,
            reason="strategy_failed",
        )


class _Src:
    """测试用 pinned source。"""

    def __init__(self, name: str, text: str | None, *, max_chars: int = 1000,
                 boom: bool = False) -> None:
        self.name = name
        self.max_chars = max_chars
        self._text = text
        self._boom = boom

    def format_for_injection(self) -> str | None:
        if self._boom:
            raise RuntimeError("render exploded")
        return self._text


async def _make_runner(
    skills_dir: Path,
    *,
    compressors: CompressionOrchestrator,
    pinned: PinnedStateRegistry | None,
    events: list,
    store: _FakeStore | None = None,
) -> TurnRunner:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: EventMsg) -> None:
        events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=SimClient(turns=[SimTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=store if store is not None else _FakeStore(),
        compressors=compressors,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=[user_message(f"m{i} " + "x" * 40, thread_id="t")
                        for i in range(4)],
        pinned_states=pinned,
    )


async def test_pinned_reinjected_at_tail_after_success(skills_dir: Path) -> None:
    """压缩成功 → pinned 项以 system_injection 追加 history 尾 + 持久化 + 事件。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("plan", "1. 完成 A\n2. 完成 B"))
    events: list = []
    store = _FakeStore()
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_NoopCompactStrategy()]),
        pinned=reg, events=events, store=store,
    )
    anchor_before = runner.cache_anchor_index
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    tail = runner.history_buffer[-1]
    assert tail.kind == "system_injection"
    assert tail.payload["source"] == "pinned:plan"
    assert "完成 A" in tail.payload["text"]
    # R5:持久化(resume 重放含 pinned 项)
    assert any(getattr(it, "id", None) == tail.id for it in store.items)
    # anchor 不被 tail 追加扰动
    assert runner.cache_anchor_index == anchor_before
    # 事件契约 D6
    ev = next(m for m in events if m.kind == "pinned_state_reinjected")
    assert ev.data["sources"] == [{"name": "plan", "chars": len(tail.payload["text"])}]
    assert ev.data["dropped"] == []
    assert ev.data["phase"] == "manual"
    assert ev.data["total_chars"] == len(tail.payload["text"])


async def test_total_budget_drops_source_and_records(skills_dir: Path) -> None:
    """三 source 总预算溢出:装不下的整体跳过,dropped 如实记录。"""
    reg = PinnedStateRegistry(total_max_chars=120)
    reg.register(_Src("first", "a" * 100))
    reg.register(_Src("second", "b" * 50))
    reg.register(_Src("third", "c" * 10))
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_NoopCompactStrategy()]),
        pinned=reg, events=events,
    )
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    sources = [it.payload["source"] for it in runner.history_buffer
               if it.kind == "system_injection"]
    assert sources == ["pinned:first", "pinned:third"]
    ev = next(m for m in events if m.kind == "pinned_state_reinjected")
    assert ev.data["dropped"] == ["second"]


async def test_render_none_skips_and_exception_warns(skills_dir: Path) -> None:
    """渲染 None 跳过;渲染异常 → EngineLog 告警 + 其余 source 正常注入。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("quiet", None))
    reg.register(_Src("bomb", None, boom=True))
    reg.register(_Src("ok", "still here"))
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_NoopCompactStrategy()]),
        pinned=reg, events=events,
    )
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    sources = [it.payload["source"] for it in runner.history_buffer
               if it.kind == "system_injection"]
    assert sources == ["pinned:ok"]
    warn = next(m for m in events if m.kind == "engine_log")
    assert "bomb" in warn.data["message"]
    # 压缩本身成功(渲染异常不传染)
    done = next(m for m in events if m.kind == "compaction_completed")
    assert done.data["success"] is True


async def test_all_none_emits_nothing(skills_dir: Path) -> None:
    """全部渲染 None → 不注入、不 emit(零噪声)。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("quiet", None))
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_NoopCompactStrategy()]),
        pinned=reg, events=events,
    )
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    assert not any(it.kind == "system_injection" for it in runner.history_buffer)
    assert not any(m.kind == "pinned_state_reinjected" for m in events)


async def test_failed_compaction_no_reinjection(skills_dir: Path) -> None:
    """压缩失败 → 不重注入、不 emit pinned 事件。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("plan", "should not appear"))
    events: list = []
    runner = await _make_runner(
        skills_dir,
        compressors=CompressionOrchestrator([_FailingStrategy()]),
        pinned=reg, events=events,
    )
    await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    assert not any(it.kind == "system_injection" for it in runner.history_buffer)
    assert not any(m.kind == "pinned_state_reinjected" for m in events)
