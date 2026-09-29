"""skill-working-set 影子模式 —— 只算分、只记录、不生效（ADR 0077）。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.event import EventMsg, SkillOutcomeRecorded, TurnStarted
from taifeng.skill.fitness import InMemorySkillFitnessStore, SkillFitnessCatalog
from taifeng.skill.fitness_shadow import ShadowEvaluation, SkillFitnessShadow
from taifeng.skill.outcome import SkillExecutionRecord
from taifeng.skill.working_set import WorkingSetPolicy
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path


def _record(
    skill_id: str, call_id: str, outcome: str, *, tokens: int = 10,
    origin: str = "whitelist", confidence: float | None = None,
) -> SkillExecutionRecord:
    return SkillExecutionRecord(
        skill_id=skill_id, call_id=call_id, parent_call_id=None, depth=1, source="user",
        trust_tier=None, selection_origin=origin,  # type: ignore[arg-type]
        selection_confidence=confidence,
        outcome=outcome, outcome_signal_source="structural",  # type: ignore[arg-type]
        end_reason="completed", error_detail=None, cost_tokens=tokens, cost_duration_ms=5,
        cost_iterations=1, ts_unix=100)


def _event(record: SkillExecutionRecord) -> EventMsg:
    return EventMsg(submission_id="sub", msg=SkillOutcomeRecorded(data=record.as_payload()))


class _Collector:
    """记录影子评估结论。"""

    def __init__(self) -> None:
        self.seen: list[ShadowEvaluation] = []

    async def on_evaluation(self, evaluation: ShadowEvaluation) -> None:
        self.seen.append(evaluation)


def _shadow(
    collector: _Collector, *, budget: int = 2,
) -> tuple[SkillFitnessShadow, InMemorySkillFitnessStore]:
    store = InMemorySkillFitnessStore()
    policy = WorkingSetPolicy(
        budget=budget, promote_min_score=0.3, promote_min_samples=3,
        quarantine_min_samples=3, quarantine_max_success_rate=0.2,
    )
    return SkillFitnessShadow(store, policy=policy, observer=collector), store


async def test_in_memory_store_is_a_catalog_and_aggregates_cost() -> None:
    store = InMemorySkillFitnessStore()
    assert isinstance(store, SkillFitnessCatalog)
    await store.record(_record("a", "c1", "success", tokens=30, origin="discovered",
                               confidence=0.9))
    await store.record(_record("a", "c2", "failure", tokens=10))
    await store.record(_record("b", "c3", "success"))

    fitness = {f.skill_id: f for f in await store.all_fitness()}

    assert sorted(fitness) == ["a", "b"]
    assert fitness["a"].cost_tokens_total == 40
    assert fitness["a"].cost_duration_ms_total == 10
    assert fitness["a"].cost_iterations_total == 2
    assert fitness["a"].discovered_selections == 1


async def test_shadow_reports_score_and_would_be_changes() -> None:
    collector = _Collector()
    shadow, _ = _shadow(collector)

    for i in range(3):
        await shadow.handle(_event(_record("good", f"g{i}", "success")))

    assert len(collector.seen) == 3
    assert [e.plan.promote for e in collector.seen] == [(), (), ("good",)]
    last = collector.seen[-1]
    assert last.skill_id == "good"
    assert last.score.decided_samples == 3
    assert last.plan.promoted == ("good",)
    # 第四次：工作集已含 good，不再重复报告提拔
    await shadow.handle(_event(_record("good", "g3", "success")))
    assert collector.seen[-1].plan.promote == ()
    assert shadow.shadow_promoted == frozenset({"good"})


async def test_shadow_flags_overpromising_skill() -> None:
    collector = _Collector()
    shadow, _ = _shadow(collector)

    for i in range(3):
        await shadow.handle(_event(_record(
            "fake", f"f{i}", "failure", origin="discovered", confidence=0.99,
        )))

    assert collector.seen[-1].plan.quarantine == ("fake",)
    assert shadow.shadow_quarantined == frozenset({"fake"})


async def test_selection_confidence_never_affects_score() -> None:
    """长相不得喂战绩：选择置信度不同、战绩相同，分数必须相同。"""
    high, low = _Collector(), _Collector()
    shadow_high, _ = _shadow(high)
    shadow_low, _ = _shadow(low)

    for i in range(4):
        await shadow_high.handle(_event(_record(
            "s", f"c{i}", "success", origin="discovered", confidence=0.99)))
        await shadow_low.handle(_event(_record(
            "s", f"c{i}", "success", origin="discovered", confidence=0.01)))

    assert high.seen[-1].score == low.seen[-1].score
    assert high.seen[-1].plan == low.seen[-1].plan


async def test_duplicate_delivery_is_not_evaluated_twice() -> None:
    collector = _Collector()
    shadow, store = _shadow(collector)
    event = _event(_record("s", "same", "success"))

    await shadow.handle(event)
    await shadow.handle(event)

    fitness = await store.fitness("s")
    assert fitness is not None and fitness.total == 1
    assert len(collector.seen) == 1


async def test_other_events_are_ignored() -> None:
    collector = _Collector()
    shadow, store = _shadow(collector)

    await shadow.handle(EventMsg(submission_id="s", msg=TurnStarted(data={})))

    assert collector.seen == []
    assert await store.all_fitness() == ()


async def test_observer_failure_propagates() -> None:
    """observer 异常原样上抛，由挂接方决定处置（与 SkillFitnessRecorder 一致）。"""

    class _Broken:
        async def on_evaluation(self, evaluation: ShadowEvaluation) -> None:
            raise RuntimeError("sink down")

    shadow = SkillFitnessShadow(
        InMemorySkillFitnessStore(), policy=WorkingSetPolicy(budget=1), observer=_Broken()
    )
    with pytest.raises(RuntimeError, match="sink down"):
        await shadow.handle(_event(_record("s", "c1", "success")))


def test_store_without_catalog_is_rejected_at_construction() -> None:
    """存储不支持遍历：构造期即报错，不等到首条事件。"""

    class _WriteOnly:
        async def record(self, record: SkillExecutionRecord) -> None:
            return None

        async def fitness(self, skill_id: str) -> None:
            return None

    with pytest.raises(TypeError, match="all_fitness"):
        SkillFitnessShadow(_WriteOnly(), policy=WorkingSetPolicy(budget=1))  # type: ignore[arg-type]


async def test_shadow_does_not_change_what_the_model_sees(
    skills_dir: Path, threads_dir: Path, tmp_path: Path,
) -> None:
    """真实 pool：挂不挂影子评估，发给模型的请求逐字节相同。"""

    def turns() -> list[SimTurn]:
        return [
            SimTurn(text="派发", tool_calls=[{
                "id": "c1", "name": "call_skill",
                "arguments": '{"skill_id": "style-checker", "reason": "查风格"}'}]),
            SimTurn(text="无违规"),
            SimTurn(text="通过"),
        ]

    async def run(name: str, attach: bool) -> tuple[list[object], _Collector]:
        client = SimClient(turns=turns())
        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir, threads_dir=tmp_path / name, model_client=client,
            compressors=[])
        engine = await pool.get_or_create(session_id=name, entry_skill_id="code-reviewer")
        collector = _Collector()
        task = None
        if attach:
            shadow, _ = _shadow(collector, budget=5)
            task = asyncio.create_task(shadow.attach(engine))
            await asyncio.sleep(0)
        kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text="审查"))
        assert kind == "turn_completed"
        if task is not None:
            for _ in range(100):
                if collector.seen:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
        await pool.close()
        requests = [
            (
                recorded.system_texts(),
                [(m.role, m.content, m.tool_calls) for m in recorded.request.messages],
                sorted(recorded.tool_names()),
            )
            for recorded in client.ledger.requests()
        ]
        return requests, collector

    plain, _ = await run("plain", attach=False)
    shadowed, collector = await run("shadowed", attach=True)

    assert [e.skill_id for e in collector.seen] == ["style-checker"]
    assert len(plain) == 3
    assert shadowed == plain
