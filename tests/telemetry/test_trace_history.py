"""JSONL/Timeline 到统一轨迹与 Mermaid 时间线的测试。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from taifeng.loop.event import EventMsg
from taifeng.loop.execution_identity import ExecutionIdentity
from taifeng.telemetry.trace_history import load_event_jsonl, mermaid_timeline, timeline_events
from taifeng.telemetry.trace_projection import TraceEvent


def _event(seq: int, kind: str) -> EventMsg:
    """构造可写入 JSONL 的事件。"""
    return EventMsg(
        submission_id="s",
        seq=seq,
        msg={"kind": kind, "data": {"name": "read_skill"}},
        execution=ExecutionIdentity(
            execution_id="root", attempt_id="a", thread_id="root",
            skill_id="entry", model="sim",
        ),
    )


def test_load_event_jsonl_replays_order(tmp_path) -> None:
    """JSONL 回放保留全局顺序和完整性。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(event.model_dump_json() for event in (_event(0, "turn_started"), _event(1, "turn_completed")))
        + "\n",
        encoding="utf-8",
    )
    snapshot = load_event_jsonl(path)
    assert [item.kind for item in snapshot.events] == ["turn_started", "turn_completed"]
    assert not snapshot.incomplete


def test_timeline_events_and_mermaid_keep_seq_and_causation() -> None:
    """Journal Timeline 可转成线性 Mermaid 轨迹。"""
    items = [SimpleNamespace(
        seq=3, record_type="tool_intent", recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        thread_id="t", submission_id="s", turn_id="turn", operation_id="op",
        causation_id=None, call_id="c", skill_id="worker", payload={"name": "read_skill"},
    )]
    events = timeline_events(items)
    assert events[0].seq == 3
    graph = mermaid_timeline(events)
    assert "flowchart LR" in graph
    assert "tool_intent" in graph
    assert "3" in graph


def test_load_event_jsonl_marks_bad_lines_and_capacity(tmp_path) -> None:
    """轨迹工具不会把坏行或截断历史伪装成完整结果。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        _event(0, "turn_started").model_dump_json()
        + "\nnot-json\n"
        + _event(2, "turn_completed").model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    snapshot = load_event_jsonl(path, max_events=1)
    assert len(snapshot.events) == 1
    assert snapshot.incomplete


def test_mermaid_escapes_labels() -> None:
    """绘图标签中的引号和换行不会改变图语法。"""
    graph = mermaid_timeline((
        TraceEvent(
            seq=0, timestamp=datetime.now(UTC), kind="tool_call_started", submission_id="s",
            execution_id="root", attempt_id="a", thread_id="root", skill_id="entry",
            parent_execution_id=None, parent_thread_id=None, call_id="c",
            metadata=(("name", 'read"skill'),),
        ),
    ))
    assert "read'skill" in graph
    assert 'read"skill' not in graph
