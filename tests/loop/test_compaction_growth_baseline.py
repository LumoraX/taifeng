"""压缩增量基线（ADR 0083）：上次压缩后没长多少，就不再压。

压缩腾出的空间有限时（保留的尾部本身就大），估算会停在软阈值之上，此后每次预算检查都
触发一次压缩：每次都破坏缓存、每次都对摘要再做摘要，却几乎腾不出空间。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from taifeng.context.budget import (
    POST_COMPACTION_TOKENS_KEY,
    ContextBudget,
    last_compaction_baseline,
    recompaction_blocked,
)
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    compacted,
    user_message,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

TID = "thr-baseline"


def _placeholder(baseline: int | None) -> ResponseItem:
    item = compacted("摘要", thread_id=TID, replaced_range=(0, 2), cache_invalidated=True)
    if baseline is None:
        return item
    return item.model_copy(update={"metadata": {POST_COMPACTION_TOKENS_KEY: baseline}})


# ------------------------------------------------------------------
# 纯函数
# ------------------------------------------------------------------


def test_baseline_is_read_from_the_last_compaction() -> None:
    history = [_placeholder(100), user_message("a", thread_id=TID), _placeholder(700)]
    assert last_compaction_baseline(history) == 700


def test_no_compaction_means_no_baseline() -> None:
    assert last_compaction_baseline([user_message("a", thread_id=TID)]) is None
    assert last_compaction_baseline([]) is None


def test_compaction_without_recorded_baseline_means_no_baseline() -> None:
    """旧 transcript 里的压缩条目没有基线：不设闸，行为与引入前一致。"""
    assert last_compaction_baseline([_placeholder(None)]) is None


@pytest.mark.parametrize("bad", ["700", -1, 1.5, True, None])
def test_malformed_baseline_is_ignored(bad: object) -> None:
    item = _placeholder(None).model_copy(update={"metadata": {POST_COMPACTION_TOKENS_KEY: bad}})
    assert last_compaction_baseline([item]) is None


def test_gate_is_off_by_default() -> None:
    budget = ContextBudget(context_window=1000)
    assert budget.recompact_min_growth_ratio == 0.0
    assert recompaction_blocked([_placeholder(900)], 901, budget) is None


def test_gate_blocks_until_history_grows_enough() -> None:
    budget = ContextBudget(context_window=1000, recompact_min_growth_ratio=0.1)
    history = [_placeholder(860)]

    blocked = recompaction_blocked(history, 900, budget)

    assert blocked is not None
    assert (blocked.baseline_tokens, blocked.required_tokens) == (860, 946)
    assert recompaction_blocked(history, 945, budget) is not None
    assert recompaction_blocked(history, 946, budget) is None


def test_hard_limit_always_overrides_the_gate() -> None:
    """已到硬阈值：不压就要溢出，基线不设闸。"""
    budget = ContextBudget(context_window=1000, recompact_min_growth_ratio=0.5)
    assert budget.hard_limit == 950
    assert recompaction_blocked([_placeholder(900)], 949, budget) is not None
    assert recompaction_blocked([_placeholder(900)], 950, budget) is None


def test_gate_needs_a_baseline() -> None:
    budget = ContextBudget(context_window=1000, recompact_min_growth_ratio=0.5)
    assert recompaction_blocked([user_message("a", thread_id=TID)], 900, budget) is None


@pytest.mark.parametrize("ratio", [-0.1, float("nan"), float("inf")])
def test_invalid_ratio_is_rejected(ratio: float) -> None:
    with pytest.raises(ValueError, match="recompact_min_growth_ratio"):
        ContextBudget(recompact_min_growth_ratio=ratio)


# ------------------------------------------------------------------
# TurnRunner
# ------------------------------------------------------------------


class _Store:
    """最小内存 store。"""

    def __init__(self) -> None:
        self.items: list[ResponseItem] = []

    async def append(self, item: ResponseItem) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


class _KeepTail:
    """恒触发的压缩：把开头若干条折叠成 placeholder，保留尾部（腾不出多少空间）。"""

    name = "keep_tail"
    priority = 10

    def __init__(self) -> None:
        self.runs = 0

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        return CompressionTrigger(reason="token_limit", threshold_pct=1.0)

    async def compress(self, ctx: CompressionContext, injection: object) -> CompressionResult:
        self.runs += 1
        start = next(
            (i for i, item in enumerate(ctx.history) if item.kind != "compacted"), 0
        )
        # 保留尾部三条：最后两条大消息 + turn 开始时注入的预算提示
        end = max(start + 1, len(ctx.history) - 3)
        placeholder = compacted(
            "摘要", thread_id=TID, replaced_range=(0, end), cache_invalidated=True,
        )
        return CompressionResult(
            success=True, cache_invalidated=True, anchor_preserved_until=-1,
            new_history=[placeholder, *ctx.history[end:]],
            removed_item_count=end, summary_item_id=placeholder.id,
        )


def _runner(
    history: list[ResponseItem], strategy: _KeepTail, budget: ContextBudget,
    events: list[Any], store: _Store, turns: int = 1,
) -> TurnRunner:
    from pathlib import Path

    entry = SkillDefinition(
        id="e", name="e", description="测试入口", version="1.0.0", type="composite",
        entry=True, body="入口", body_path=Path("_test_e.md"),
        child_skills=frozenset(), tool_names=frozenset({"read_skill"}), max_call_depth=3,
    )

    async def emit(ev: Any) -> None:
        events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=SkillSnapshot(version=1, skills=(entry,)),
        model_client=SimClient(turns=[SimTurn(text="答") for _ in range(turns)]),
        tool_runtime=ToolCallRuntime(ToolRegistry([])),
        store=store,
        compressors=CompressionOrchestrator([strategy]),
        dispatch_policy=DispatchPolicy(),
        budget=budget,
        thread_id=TID,
        submission_id="s",
        emit=emit,
        cancel=CancellationToken(name="t"),
        history_buffer=history,
    )


def _budget(ratio: float = 0.0) -> ContextBudget:
    """软阈值 750、硬阈值 1485：尾部两条大消息（约 800）落在两者之间。"""
    return ContextBudget(
        context_window=1500, soft_limit_ratio=0.5, hard_limit_ratio=0.99,
        recompact_min_growth_ratio=ratio,
    )


def _long_history() -> list[ResponseItem]:
    """尾部两条就超过软阈值：压缩后估算仍在阈值之上。"""
    big = "x" * 1400
    return [
        user_message("一 " + big, thread_id=TID),
        assistant_message("答一 " + big, thread_id=TID, model="m"),
        user_message("二 " + big, thread_id=TID),
        assistant_message("答二 " + big, thread_id=TID, model="m"),
        user_message("三 " + big, thread_id=TID),
    ]


async def _run_turn(
    history: list[ResponseItem], strategy: _KeepTail, budget: ContextBudget,
) -> tuple[list[Any], _Store]:
    events: list[Any] = []
    store = _Store()
    outcome = await _runner(history, strategy, budget, events, store).run()
    assert outcome.success
    return events, store


async def test_compaction_stamps_its_baseline_on_the_placeholder() -> None:
    history = _long_history()
    budget = _budget(0.2)

    _, store = await _run_turn(history, _KeepTail(), budget)

    placeholder = next(i for i in history if i.kind == "compacted")
    baseline = placeholder.metadata[POST_COMPACTION_TOKENS_KEY]
    assert isinstance(baseline, int)
    assert budget.soft_limit <= baseline < budget.hard_limit
    persisted = next(i for i in store.items if i.kind == "compacted")
    assert persisted.id == placeholder.id
    assert persisted.metadata[POST_COMPACTION_TOKENS_KEY] == baseline
    assert last_compaction_baseline(history) == baseline


async def test_baseline_is_stamped_even_when_the_gate_is_off() -> None:
    """基线总是记录：之后打开闸门时，已有的压缩就有据可依。"""
    history = _long_history()

    await _run_turn(history, _KeepTail(), _budget())

    assert last_compaction_baseline(history) is not None


async def test_without_the_gate_every_check_compacts_again() -> None:
    history = _long_history()
    strategy = _KeepTail()
    budget = _budget()

    await _run_turn(history, strategy, budget)
    history.append(user_message("四", thread_id=TID))
    await _run_turn(history, strategy, budget)

    assert strategy.runs >= 2


async def test_gate_defers_recompaction_and_reports_it() -> None:
    history = _long_history()
    strategy = _KeepTail()
    budget = _budget(0.2)

    await _run_turn(history, strategy, budget)
    runs_after_first = strategy.runs
    baseline = last_compaction_baseline(history)
    history.append(user_message("四", thread_id=TID))
    events, _ = await _run_turn(history, strategy, budget)

    assert runs_after_first == 1
    assert strategy.runs == 1
    deferred = [e for e in events if e.kind == "compaction_deferred"]
    assert deferred
    data = deferred[0].data
    assert data["reason"] == "below_growth_baseline"
    assert data["baseline_tokens"] == baseline
    assert data["required_tokens"] == baseline + math.ceil(round(baseline * 0.2, 6))
    assert baseline <= data["token_estimate"] < data["required_tokens"]
    assert data["phase"] in ("pre_turn", "mid_turn")
    assert not [e for e in events if e.kind == "compaction_started"]


async def test_gate_opens_once_history_has_grown() -> None:
    history = _long_history()
    strategy = _KeepTail()
    budget = _budget(0.2)

    await _run_turn(history, strategy, budget)
    assert strategy.runs == 1
    history.append(user_message("四 " + "y" * 2000, thread_id=TID))
    await _run_turn(history, strategy, budget)

    assert strategy.runs == 2


async def test_forced_compaction_ignores_the_gate() -> None:
    history = _long_history()
    strategy = _KeepTail()
    budget = _budget(0.5)
    events: list[Any] = []
    await _run_turn(history, strategy, budget)

    runner = _runner(history, strategy, budget, events, _Store())
    applied = await runner._maybe_compress(phase="manual", force=True)  # noqa: SLF001

    assert applied is True
    assert strategy.runs == 2
    assert not [e for e in events if e.kind == "compaction_deferred"]


async def test_growth_ratio_is_adjustable_at_runtime(tmp_path: Any) -> None:
    import taifeng
    from taifeng.loop.submission import UpdateBudget
    from tests.conftest import ATOMIC_SKILL, wait_for_condition

    entry = """---
name: chat
description: 对话入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
max_call_depth: 2
---
# 入口
"""
    for name, body in (("chat", entry), ("style-checker", ATOMIC_SKILL)):
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").write_text(body, encoding="utf-8")
    pool = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[]), compressors=[],
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="chat")
    assert engine.budget.recompact_min_growth_ratio == 0.0

    await engine.submit(UpdateBudget(recompact_min_growth_ratio=0.25))
    await wait_for_condition(
        lambda: engine.budget.recompact_min_growth_ratio == 0.25,
        message="UpdateBudget 未生效",
    )
    # 非法值被拒，保持原值
    await engine.submit(UpdateBudget(recompact_min_growth_ratio=-1.0))
    await engine.submit(UpdateBudget(context_window=123_456))
    await wait_for_condition(
        lambda: engine.budget.context_window == 123_456, message="UpdateBudget 未生效"
    )

    assert engine.budget.recompact_min_growth_ratio == 0.25
    await pool.close()
