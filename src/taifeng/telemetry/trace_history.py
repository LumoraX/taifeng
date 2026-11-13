"""把事件 JSONL 或 Journal Timeline 转成可绘制的调用历史。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from taifeng.loop.event import EventMsg

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


@dataclass(frozen=True)
class TraceEvent:
    """不携带正文的统一轨迹事实。"""

    seq: int
    kind: str
    recorded_at: datetime
    submission_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None
    parent_execution_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    operation_id: str | None = None
    record_id: str | None = None
    causation_id: str | None = None
    call_id: str | None = None
    skill_id: str | None = None
    name: str | None = None
    phase: str | None = None
    end_reason: str | None = None


@dataclass(frozen=True)
class TraceSnapshot:
    """JSONL 轨迹读取结果及完整性标记。"""

    events: tuple[TraceEvent, ...]
    incomplete: bool = False


def _text(value: object) -> str | None:
    """只保留有限字符串字段。"""
    return value if isinstance(value, str) and value else None


def _event_from_msg(event: EventMsg) -> TraceEvent:
    """将 EventMsg 映射成脱正文事实。"""
    data = event.msg.data
    identity = event.execution
    return TraceEvent(
        seq=event.seq,
        kind=event.msg.kind,
        recorded_at=event.timestamp,
        submission_id=event.submission_id,
        execution_id=identity.execution_id if identity else None,
        attempt_id=identity.attempt_id if identity else None,
        parent_execution_id=identity.parent_execution_id if identity else None,
        thread_id=identity.thread_id if identity else None,
        call_id=_text(data.get("call_id")),
        skill_id=_text(data.get("skill_id")),
        name=_text(data.get("name")),
        phase=_text(data.get("phase")),
        end_reason=_text(data.get("end_reason")),
    )


def load_event_jsonl(path: Path, *, max_events: int = 8192) -> TraceSnapshot:
    """读取 EventMsg JSONL；坏行、跳号和容量截断均显式标记不完整。"""
    if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events <= 0:
        raise ValueError("max_events must be a positive integer")
    events: list[TraceEvent] = []
    incomplete = False
    previous_seq: int | None = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = EventMsg.model_validate_json(line)
            except (TypeError, ValueError):
                incomplete = True
                continue
            if previous_seq is None:
                incomplete |= event.seq != 0
            elif event.seq != previous_seq + 1:
                incomplete = True
            previous_seq = event.seq
            if len(events) >= max_events:
                incomplete = True
                continue
            events.append(_event_from_msg(event))
    return TraceSnapshot(events=tuple(events), incomplete=incomplete)


def timeline_events(items: Iterable[object]) -> tuple[TraceEvent, ...]:
    """把 Journal Timeline item 映射为同一套轨迹事实。"""
    result: list[TraceEvent] = []
    for item in items:
        payload = getattr(item, "payload", {})
        if not isinstance(payload, dict):
            payload = {}
        recorded_at = getattr(item, "recorded_at", None)
        if not isinstance(recorded_at, datetime):
            recorded_at = datetime.now(UTC)
        result.append(
            TraceEvent(
                seq=int(getattr(item, "seq", 0)),
                kind=str(getattr(item, "record_type", "unknown")),
                recorded_at=recorded_at,
                submission_id=_text(getattr(item, "submission_id", None)),
                thread_id=_text(getattr(item, "thread_id", None)),
                turn_id=_text(getattr(item, "turn_id", None)),
                operation_id=_text(getattr(item, "operation_id", None)),
                record_id=_text(getattr(item, "record_id", None)),
                causation_id=_text(getattr(item, "causation_id", None)),
                call_id=_text(getattr(item, "call_id", None)) or _text(payload.get("call_id")),
                skill_id=_text(getattr(item, "skill_id", None)) or _text(payload.get("skill_id")),
                name=_text(payload.get("name")),
                phase=_text(payload.get("phase")),
                end_reason=_text(payload.get("end_reason")),
            )
        )
    return tuple(result)


def _escape(value: str) -> str:
    """转义 Mermaid 标签中的控制字符。"""
    return value.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def mermaid_timeline(events: Iterable[TraceEvent]) -> str:
    """生成可直接粘贴到 Mermaid 的线性调用历史图。"""
    materialized = tuple(events)
    lines = ["flowchart LR"]
    ids: dict[str, str] = {}
    for index, event in enumerate(materialized):
        node_id = f"e{index}"
        if event.record_id is not None:
            ids[event.record_id] = node_id
        label = f"{event.seq} {event.kind}"
        if event.name is not None:
            label += f"\\n{event.name}"
        elif event.skill_id is not None:
            label += f"\\n{event.skill_id}"
        lines.append(f'    {node_id}["{_escape(label)}"]')
    for index in range(1, len(materialized)):
        lines.append(f"    e{index - 1} --> e{index}")
    for index, event in enumerate(materialized):
        if event.causation_id in ids:
            lines.append(f"    {ids[event.causation_id]} -. causation .-> e{index}")
    return "\n".join(lines)


__all__ = ["TraceEvent", "TraceSnapshot", "load_event_jsonl", "mermaid_timeline", "timeline_events"]
