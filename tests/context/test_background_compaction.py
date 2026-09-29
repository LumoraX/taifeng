"""BackgroundCompactionStrategy —— 后台延迟压缩（ADR 0087）。

到软阈值时先在后台算摘要、不阻塞当前 turn；下一轮开始时前缀未变就直接应用。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionStrategy,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import BackgroundCompactionStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    compacted,
    user_message,
)

TID = "t-bg"
BLUM = InitialContextInjection.BEFORE_LAST_USER_MESSAGE
DNI = InitialContextInjection.DO_NOT_INJECT


class _Slow:
    """可控的压缩策略：``release`` 之前一直在「算摘要」。"""

    name = "slow"
    priority = 10

    def __init__(self, *, fail: bool = False, error: BaseException | None = None) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[tuple[str, int]] = []
        self._fail = fail
        self._error = error

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        return CompressionTrigger(reason="token_limit", threshold_pct=0.7)

    async def compress(self, ctx: CompressionContext, injection: Any) -> CompressionResult:
        self.calls.append((ctx.phase, len(ctx.history)))
        self.started.set()
        await self.release.wait()
        if self._error is not None:
            raise self._error
        if self._fail:
            return CompressionResult(
                success=False, cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index, reason="summary_failed",
            )
        keep = ctx.history[-2:]
        placeholder = compacted(
            "摘要", thread_id=ctx.history[0].thread_id,
            replaced_range=(0, len(ctx.history) - 2), cache_invalidated=True,
        )
        return CompressionResult(
            success=True, cache_invalidated=True, anchor_preserved_until=-1,
            new_history=[placeholder, *keep],
            removed_item_count=len(ctx.history) - 2, summary_item_id=placeholder.id,
        )


def _history(n: int = 6) -> list[ResponseItem]:
    items: list[ResponseItem] = []
    for i in range(n // 2):
        items.append(user_message(f"问{i}", thread_id=TID))
        items.append(assistant_message(f"答{i}", thread_id=TID, model="m"))
    return items


def _ctx(
    history: list[ResponseItem], *, ratio: float = 0.7, phase: str = "pre_turn",
    anchor: int = -1,
) -> CompressionContext:
    return CompressionContext(
        history=history,
        token_estimate=int(10_000 * ratio),
        budget=ContextBudget(context_window=10_000, soft_limit_ratio=0.6),
        cache_anchor_index=anchor,
        phase=phase,  # type: ignore[arg-type]
        available_injections=frozenset({BLUM}),
    )


def _strategy(inner: _Slow, **kwargs: Any) -> BackgroundCompactionStrategy:
    return BackgroundCompactionStrategy([inner], **{"urgent_ratio": 0.85, **kwargs})


async def _settle(strategy: BackgroundCompactionStrategy) -> None:
    """等后台任务收尾（结果进入待应用状态）。"""
    await strategy.wait_idle()


def test_is_a_compression_strategy() -> None:
    strategy = _strategy(_Slow())
    assert isinstance(strategy, CompressionStrategy)
    assert strategy.name == "background"


async def test_first_check_starts_background_work_without_blocking() -> None:
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()

    assert strategy.should_trigger(_ctx(history)) is None
    await inner.started.wait()

    assert inner.calls == [("pre_turn", 6)]
    assert strategy.pending_threads == frozenset({TID})
    inner.release.set()
    await strategy.aclose()


async def test_ready_result_is_applied_at_the_next_pre_turn() -> None:
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    await inner.started.wait()
    # 后台计算期间又多了一问一答
    grown = [*history, user_message("问3", thread_id=TID),
             assistant_message("答3", thread_id=TID, model="m")]
    inner.release.set()
    await _settle(strategy)

    trigger = strategy.should_trigger(_ctx(grown))
    assert trigger is not None
    result = await strategy.compress(_ctx(grown), BLUM)

    assert result.success
    kinds = [i.kind for i in result.new_history]
    assert kinds == ["compacted", "user_message", "assistant_message",
                     "user_message", "assistant_message"]
    # 保留的尾部与后来追加的条目原样（同一对象身份）
    assert [i.id for i in result.new_history[1:]] == [i.id for i in grown[4:]]
    assert result.summary_item_id == result.new_history[0].id
    assert result.detail["background"] == 1
    assert inner.calls == [("pre_turn", 6)], "应用时不得再次计算"
    assert strategy.pending_threads == frozenset()
    await strategy.aclose()


async def test_result_is_not_applied_mid_turn() -> None:
    """后台结果按 pre_turn 语义算出（可动 head）；mid_turn 不得应用。"""
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    inner.release.set()
    await _settle(strategy)

    assert strategy.should_trigger(_ctx(history, phase="mid_turn", anchor=3)) is None
    assert strategy.should_trigger(_ctx(history)) is not None
    await strategy.aclose()


async def test_stale_result_is_discarded_when_the_prefix_changed() -> None:
    """计算期间 history 被 rewind / 另一次压缩改写：结果作废，重新开始。"""
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    await inner.started.wait()
    inner.release.set()
    await _settle(strategy)
    rewound = [*history[:3], user_message("改问", thread_id=TID)]
    inner.started.clear()

    assert strategy.should_trigger(_ctx(rewound)) is None
    await inner.started.wait()

    assert inner.calls == [("pre_turn", 6), ("pre_turn", 4)]
    await strategy.aclose()


async def test_urgent_pressure_compacts_synchronously() -> None:
    """已逼近硬阈值：等不起，走同步压缩；在算的后台任务作废。"""
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    await inner.started.wait()

    urgent = _ctx(history, ratio=0.9)
    assert strategy.should_trigger(urgent) is not None
    inner.release.set()
    result = await strategy.compress(urgent, BLUM)

    assert result.success
    assert result.detail.get("background", 0) == 0
    assert strategy.pending_threads == frozenset()
    await strategy.aclose()


async def test_mid_turn_never_starts_background_work() -> None:
    inner = _Slow()
    strategy = _strategy(inner)

    assert strategy.should_trigger(_ctx(_history(), phase="mid_turn", anchor=1)) is None
    await asyncio.sleep(0)

    assert inner.calls == []
    assert strategy.pending_threads == frozenset()
    await strategy.aclose()


async def test_failed_background_result_falls_back_to_synchronous() -> None:
    inner = _Slow(fail=True)
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    inner.release.set()
    await _settle(strategy)

    # 后台失败过一次：下一次检查不再后台重试，直接同步（结果如实返回失败）
    trigger = strategy.should_trigger(_ctx(history))
    assert trigger is not None
    result = await strategy.compress(_ctx(history), BLUM)

    assert result.success is False
    assert result.reason == "summary_failed"
    assert len(inner.calls) == 2
    await strategy.aclose()


async def test_background_exception_is_logged_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = _Slow(error=RuntimeError("summary model down"))
    strategy = _strategy(inner)
    history = _history()
    strategy.should_trigger(_ctx(history))
    inner.release.set()
    await _settle(strategy)

    assert any("summary model down" in str(r.exc_info) for r in caplog.records if r.exc_info)
    assert strategy.should_trigger(_ctx(history)) is not None
    await strategy.aclose()


async def test_only_one_background_task_per_thread() -> None:
    inner = _Slow()
    strategy = _strategy(inner)
    history = _history()

    for _ in range(3):
        assert strategy.should_trigger(_ctx(history)) is None
    await inner.started.wait()

    assert len(inner.calls) == 1
    inner.release.set()
    await strategy.aclose()


async def test_threads_are_isolated() -> None:
    inner = _Slow()
    strategy = _strategy(inner)
    other = [i.model_copy(update={"thread_id": "t-other"}) for i in _history()]

    strategy.should_trigger(_ctx(_history()))
    strategy.should_trigger(_ctx(other))
    await inner.started.wait()
    await asyncio.sleep(0)

    assert strategy.pending_threads == frozenset({TID, "t-other"})
    inner.release.set()
    await strategy.aclose()


async def test_nothing_to_do_when_inner_does_not_trigger() -> None:
    class _Quiet(_Slow):
        def should_trigger(self, ctx: CompressionContext) -> None:
            return None

    inner = _Quiet()
    strategy = _strategy(inner)

    assert strategy.should_trigger(_ctx(_history())) is None
    assert strategy.should_trigger(_ctx(_history(), ratio=0.9)) is None
    assert inner.calls == []
    await strategy.aclose()


async def test_empty_history() -> None:
    strategy = _strategy(_Slow())
    assert strategy.should_trigger(_ctx([])) is None
    await strategy.aclose()


async def test_aclose_cancels_running_work() -> None:
    inner = _Slow()
    strategy = _strategy(inner)
    strategy.should_trigger(_ctx(_history()))
    await inner.started.wait()

    await strategy.aclose()

    assert strategy.pending_threads == frozenset()
    # 关闭后不再起后台任务，退回同步
    assert strategy.should_trigger(_ctx(_history())) is not None


async def test_forced_compress_without_a_ready_result_runs_synchronously() -> None:
    """overflow 自愈走 force_compress：没有现成结果时直接同步压缩。"""
    inner = _Slow()
    inner.release.set()
    strategy = _strategy(inner)
    orchestrator = CompressionOrchestrator([strategy])

    result = await orchestrator.force_compress(_ctx(_history(), phase="overflow"), DNI)

    assert result is not None and result.success
    assert inner.calls == [("overflow", 6)]
    await strategy.aclose()


@pytest.mark.parametrize("kwargs", [{"urgent_ratio": 0.0}, {"urgent_ratio": 1.5}])
def test_invalid_parameters_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        BackgroundCompactionStrategy([_Slow()], **kwargs)


def test_requires_at_least_one_inner_strategy() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BackgroundCompactionStrategy([])


# ------------------------------------------------------------------
# 引擎级
# ------------------------------------------------------------------


async def test_turn_is_not_blocked_and_next_turn_applies_the_result(tmp_path: Any) -> None:
    import taifeng
    from taifeng.context.budget import last_compaction_baseline
    from taifeng.llm.providers import SimClient, SimTurn
    from taifeng.llm.types import TokenUsage
    from tests.conftest import ATOMIC_SKILL, GUARD_TIMEOUT_SECONDS

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
    inner = _Slow()
    strategy = BackgroundCompactionStrategy([inner], urgent_ratio=0.9)
    pool = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", threads_dir=tmp_path / "threads",
        # 估算以 provider 回报的用量校准：让回报值落在软阈值（600）与紧急线（1800）之间
        model_client=SimClient(turns=[
            SimTurn(text=f"答{i} " + "x" * 700,
                    usage=TokenUsage(input_tokens=900, output_tokens=50))
            for i in range(4)
        ]),
        compressors=[strategy],
        budget=ContextBudget(context_window=2000, soft_limit_ratio=0.3, hard_limit_ratio=0.95),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="chat")

    async def turn(text: str) -> list[str]:
        sub_id = await engine.submit(taifeng.UserMessage(text=text))

        async def collect() -> list[str]:
            kinds: list[str] = []
            async for ev in engine.subscribe(sub_id):
                kinds.append(ev.msg.kind)
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    return kinds
            raise AssertionError("stream ended early")

        return await asyncio.wait_for(collect(), timeout=GUARD_TIMEOUT_SECONDS)

    # 前三轮把上下文推过软阈值：后台开始算摘要（永不放行），但每一轮都照常完成
    for i in range(3):
        kinds = await turn(f"问{i} " + "y" * 700)
        assert kinds[-1] == "turn_completed"
    assert inner.started.is_set(), "后台压缩应已开始"
    assert not any(i.kind == "compacted" for i in engine.history_snapshot())

    # 摘要算完；下一轮开始时应用，不再等模型
    inner.release.set()
    await strategy.wait_idle()
    calls_before = len(inner.calls)
    kinds = await turn("问3")

    assert kinds[-1] == "turn_completed"
    assert "compaction_completed" in kinds
    assert len(inner.calls) == calls_before, "应用时不得再次计算"
    history = engine.history_snapshot()
    assert history[0].kind == "compacted"
    assert last_compaction_baseline(history) is not None
    stored = [i async for i in await pool.store.load_thread(engine.thread_id)]
    assert [i.id for i in stored if i.kind == "compacted"] == [history[0].id]

    await pool.close()
    assert strategy.pending_threads == frozenset()
