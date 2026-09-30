"""skill-fitness —— 战绩事件经 SkillFitnessRecorder 聚合进 SkillFitnessStore。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.skill.fitness import (
    InMemorySkillFitnessStore,
    SkillFitness,
    SkillFitnessRecorder,
    SkillFitnessStore,
)
from taifeng.skill.outcome import SkillExecutionRecord
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path


def _record(call_id: str, outcome: str, ts: int = 100) -> SkillExecutionRecord:
    return SkillExecutionRecord(
        skill_id="style-checker", call_id=call_id, parent_call_id=None, depth=1, source="user",
        trust_tier=None, selection_origin="whitelist", selection_confidence=None,
        outcome=outcome, outcome_signal_source="structural",  # type: ignore[arg-type]
        end_reason="completed", error_detail=None, cost_tokens=10, cost_duration_ms=5,
        cost_iterations=1, ts_unix=ts)


def test_record_roundtrips_through_payload() -> None:
    record = _record("c1", "success")
    assert SkillExecutionRecord.from_payload(record.as_payload()) == record


def test_from_payload_missing_field_fails_loudly() -> None:
    payload = _record("c1", "success").as_payload()
    del payload["outcome"]
    with pytest.raises(KeyError):
        SkillExecutionRecord.from_payload(payload)


async def test_in_memory_store_counts_and_dedupes() -> None:
    store = InMemorySkillFitnessStore()
    assert isinstance(store, SkillFitnessStore)
    for call_id, outcome, ts in [("a", "success", 1), ("b", "failure", 3), ("c", "abandoned", 2),
                                 ("a", "success", 9)]:
        await store.record(_record(call_id, outcome, ts))
    assert await store.fitness("style-checker") == SkillFitness(
        skill_id="style-checker", successes=1, failures=1, abandoned=1, last_ts_unix=3,
        cost_tokens_total=30, cost_duration_ms_total=15, cost_iterations_total=3)
    assert await store.fitness("unknown") is None


async def test_recorder_aggregates_real_dispatch(skills_dir: Path, threads_dir: Path) -> None:
    """真实 pool：call_skill 子 skill 终态 → 事件 → recorder → store 聚合。"""
    store = InMemorySkillFitnessStore()
    client = SimClient(turns=[
        SimTurn(text="派发", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "查风格"}'}]),
        SimTurn(text="无违规"),
        SimTurn(text="通过"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="fit", entry_skill_id="code-reviewer")
    task = asyncio.create_task(SkillFitnessRecorder(store).attach(engine))
    await asyncio.sleep(0)  # 让 recorder 先订阅上
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="审查")) == "turn_completed"
    for _ in range(100):
        if await store.fitness("style-checker") is not None:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await pool.close()
    fitness = await store.fitness("style-checker")
    assert fitness is not None and (fitness.successes, fitness.total) == (1, 1)
