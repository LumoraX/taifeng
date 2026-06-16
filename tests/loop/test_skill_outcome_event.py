"""SkillOutcomeRecorded 事件单元测试。"""
from __future__ import annotations

from taifeng.loop.event import SkillOutcomeRecorded


def test_skill_outcome_recorded_event():
    """新事件 kind 固定为 skill_outcome_recorded，data 透传记录 payload。"""
    ev = SkillOutcomeRecorded(data={"skill_id": "metabolic", "outcome": "success"})
    assert ev.kind == "skill_outcome_recorded"
    assert ev.data["outcome"] == "success"
