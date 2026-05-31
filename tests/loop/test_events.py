"""EventMsg 子类构造 / 序列化测试。"""
from __future__ import annotations

from taifeng.loop.event import EventMsg, ToolBatchDispatched


def test_tool_batch_dispatched_serializes() -> None:
    """ToolBatchDispatched 应能构造、带 count/max_parallel、并入 EventMsg。"""
    msg = ToolBatchDispatched(data={"count": 3, "max_parallel": 8})
    assert msg.kind == "tool_batch_dispatched"
    assert msg.data["count"] == 3
    ev = EventMsg(submission_id="sub-1", msg=msg)
    dumped = ev.model_dump()
    assert dumped["msg"]["kind"] == "tool_batch_dispatched"
    assert dumped["msg"]["data"]["max_parallel"] == 8


def test_orchestration_plan_resolved_event() -> None:
    """orchestration_plan_resolved 可构造 + 经 EventMsg 包装 + discriminator 正确。"""
    from taifeng.loop.event import EventMsg, OrchestrationPlanResolved

    msg = OrchestrationPlanResolved(
        data={"skill_id": "trip", "groups": [{"type": "parallel", "skills": ["a", "b"]}]}
    )
    ev = EventMsg(submission_id="s1", msg=msg)
    assert ev.msg.kind == "orchestration_plan_resolved"
    assert ev.msg.data["skill_id"] == "trip"
    assert ev.msg.data["groups"][0]["type"] == "parallel"


def test_orchestration_condition_missing_event() -> None:
    """orchestration_condition_missing 可构造 + 包装。"""
    from taifeng.loop.event import EventMsg, OrchestrationConditionMissing

    msg = OrchestrationConditionMissing(data={"skill_id": "trip", "condition": "needs_weather"})
    ev = EventMsg(submission_id="s1", msg=msg)
    assert ev.msg.kind == "orchestration_condition_missing"
    assert ev.msg.data["condition"] == "needs_weather"
