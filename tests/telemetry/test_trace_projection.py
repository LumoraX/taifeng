"""统一轨迹投影的顺序、脱敏和有界性测试。"""
from __future__ import annotations

from typing import Any

import pytest

from taifeng.loop.event import EventMsg
from taifeng.loop.execution_identity import ExecutionIdentity
from taifeng.telemetry.trace_projection import TraceProjection


def event(kind: str, seq: int, *, node: str = "root", **data: Any) -> EventMsg:
    """构造带执行身份的轻量轨迹事实。"""
    return EventMsg.model_validate({
        "submission_id": "s",
        "seq": seq,
        "msg": {"kind": kind, "data": data},
        "execution": ExecutionIdentity(
            execution_id=node, attempt_id="a", thread_id=node,
            skill_id="skill", model="sim",
        ),
    })


def test_trace_projection_preserves_global_sequence_and_redacts_body() -> None:
    """全局 seq 保留，正文不会进入轨迹投影。"""
    projection = TraceProjection()
    projection.handle(event(
        "tool_call_started", 0, call_id="c1", name="read_skill",
        arguments="secret arguments",
    ))
    projection.handle(event(
        "tool_call_completed", 1, call_id="c1", name="read_skill",
        output="secret output", duration_ms=12,
    ))
    snapshot = projection.snapshot()
    assert [item.seq for item in snapshot.events] == [0, 1]
    assert "arguments" not in snapshot.events[0].metadata
    assert "output" not in snapshot.events[1].metadata
    assert dict(snapshot.events[1].metadata)["duration_ms"] == 12


def test_trace_projection_marks_gaps_duplicates_and_capacity() -> None:
    """序号缺口、迟到事件和容量截断都显式标记不完整。"""
    projection = TraceProjection(max_events=1)
    projection.handle(event("turn_started", 0))
    projection.handle(event("turn_completed", 2))
    projection.handle(event("turn_failed", 1))
    snapshot = projection.snapshot()
    assert len(snapshot.events) == 1
    assert snapshot.incomplete


def test_trace_projection_rejects_invalid_capacity_and_renders_text() -> None:
    """容量校验和基础全局文本轨迹可用。"""
    with pytest.raises(ValueError):
        TraceProjection(max_events=0)
    projection = TraceProjection()
    projection.handle(event("turn_started", 0, node="root"))
    projection.handle(event("skill_dispatched", 1, node="root", skill_id="worker"))
    text = projection.render_text()
    assert "0000 turn_started" in text
    assert "0001 skill_dispatched" in text
    assert "worker" in text
