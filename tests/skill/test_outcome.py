"""skill/outcome.py 单元测试：数据契约形状 + 默认 judge 映射 + 协议。"""
from __future__ import annotations

import dataclasses

import pytest

from taifeng.skill.outcome import (
    OutcomeVerdict,
    SkillExecutionContext,
    SkillExecutionRecord,
    StructuralOutcomeJudge,
)


def test_record_as_payload_shape():
    """SkillExecutionRecord.as_payload() 必须含全部字段，且把长相/战绩分开存。"""
    rec = SkillExecutionRecord(
        skill_id="metabolic",
        call_id="call_1",
        parent_call_id="call_0",
        depth=1,
        source="user",
        trust_tier=None,
        selection_origin="whitelist",
        selection_confidence=None,
        outcome="success",
        outcome_signal_source="structural",
        end_reason="completed",
        error_detail=None,
        cost_tokens=120,
        cost_duration_ms=42,
        cost_iterations=3,
        ts_unix=1718000000,
    )
    payload = rec.as_payload()
    assert payload["outcome"] == "success"
    assert payload["outcome_signal_source"] == "structural"
    assert payload["selection_origin"] == "whitelist"
    assert payload["selection_confidence"] is None
    assert payload["cost_tokens"] == 120
    assert payload["cost_iterations"] == 3
    assert payload["end_reason"] == "completed"
    assert payload["ts_unix"] == 1718000000
    assert set(payload.keys()) == {
        "skill_id", "call_id", "parent_call_id", "depth", "source",
        "trust_tier", "selection_origin", "selection_confidence",
        "outcome", "outcome_signal_source", "end_reason", "error_detail",
        "cost_tokens", "cost_duration_ms", "cost_iterations", "ts_unix",
    }


def test_verdict_and_context_are_frozen():
    """OutcomeVerdict / SkillExecutionContext 不可变（frozen dataclass）。"""
    v = OutcomeVerdict(
        status="failure", reason="errored", signal_source="structural"
    )
    ctx = SkillExecutionContext(success=False, end_reason="error", error="boom")
    assert dataclasses.is_dataclass(v) and dataclasses.is_dataclass(ctx)
    try:
        v.status = "success"  # type: ignore[misc]
        raise AssertionError("OutcomeVerdict 应为 frozen")
    except dataclasses.FrozenInstanceError:
        pass


@pytest.mark.parametrize(
    "success,end_reason,expected",
    [
        (True, "completed", "success"),
        (False, "error", "failure"),
        (False, "cancelled", "abandoned"),
        (False, "max_iterations", "abandoned"),
        (False, "resource_limit_exceeded", "abandoned"),
        (False, "denial_circuit_open", "abandoned"),
        (False, "doom_loop_circuit_open", "abandoned"),
        (False, "unknown_future_reason", "failure"),
    ],
)
def test_structural_judge_mapping(success: bool, end_reason: str, expected: str) -> None:
    """默认结构性判定：success→success；放弃集→abandoned；其余非成功→failure。"""
    judge = StructuralOutcomeJudge()
    verdict = judge.judge(
        SkillExecutionContext(success=success, end_reason=end_reason, error=None)
    )
    assert verdict.status == expected
    assert verdict.signal_source == "structural"
    assert verdict.reason
