"""把 JSONL 和 Journal Timeline 转成可回放的轻量轨迹。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from taifeng.telemetry.trace_projection import TraceEvent, TraceProjection, TraceSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class TraceWaterfallRow:
    """一个 execution/attempt 的 turn 生命周期摘要。"""

    execution_id: str
    attempt_id: str | None
    skill_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str


def load_event_jsonl(path: str | Path, *, max_events: int = 8192) -> TraceSnapshot:
    """逐行回放 EventMsg JSONL；坏行会让结果明确标记不完整。"""
    projection = TraceProjection(max_events=max_events)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            from taifeng.loop.event import EventMsg

            event = EventMsg.model_validate(json.loads(line))
        except (TypeError, ValueError, json.JSONDecodeError):
            projection.incomplete = True
            continue
        projection.handle(event)
    return projection.snapshot()


def timeline_events(items: Iterable[object]) -> tuple[TraceEvent, ...]:
    """把 Journal TimelineItem 映射为线性轻量事件，不推测父子关系。"""
    events: list[TraceEvent] = []
    for item in items:
        payload = getattr(item, "payload", {})
        payload = payload if isinstance(payload, dict) else {}
        metadata: list[tuple[str, str | int | float | bool]] = [
            ("record_type", str(getattr(item, "record_type", "unknown"))),
        ]
        for key in ("operation_id", "causation_id"):
            value = getattr(item, key, None)
            if isinstance(value, str):
                metadata.append((key, value[:256]))
        events.append(
            TraceEvent(
                seq=int(getattr(item, "seq", 0)),
                timestamp=getattr(item, "recorded_at"),
                kind=str(getattr(item, "record_type", "unknown")),
                submission_id=str(getattr(item, "submission_id", "") or ""),
                execution_id=getattr(item, "thread_id", None),
                attempt_id=getattr(item, "turn_id", None) or getattr(item, "operation_id", None),
                thread_id=getattr(item, "thread_id", None),
                skill_id=getattr(item, "skill_id", None),
                parent_execution_id=None,
                parent_thread_id=None,
                call_id=getattr(item, "call_id", None),
                metadata=tuple(
                    metadata
                    + [("payload_keys", ",".join(sorted(str(key) for key in payload)[:16]))]
                ),
            )
        )
    return tuple(events)


def mermaid_timeline(events: Iterable[TraceEvent], *, max_nodes: int = 512) -> str:
    """生成按 seq 排列的 Mermaid 线性轨迹；超限显式截断。"""
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes <= 0:
        raise ValueError("max_nodes must be positive")
    all_events = list(events)
    selected = all_events[:max_nodes]
    lines = ["flowchart LR"]
    record_nodes: dict[str, str] = {}
    for index, event in enumerate(selected):
        metadata = dict(event.metadata)
        detail = metadata.get("name") or metadata.get("skill_id") or metadata.get("call_id")
        suffix = f" {detail}" if detail is not None else ""
        label = f"{event.seq} {event.kind}{suffix} [{event.execution_id or '-'}]"
        safe = label.replace('"', "'").replace("\n", " ")[:180]
        lines.append(f'  e{index}["{safe}"]')
        if index:
            lines.append(f"  e{index - 1} --> e{index}")
        for key, value in event.metadata:
            if key == "operation_id" and isinstance(value, str):
                record_nodes[value] = f"e{index}"
    for index, event in enumerate(selected):
        for key, value in event.metadata:
            if key == "causation_id" and isinstance(value, str) and value in record_nodes:
                lines.append(f"  {record_nodes[value]} -. causation .-> e{index}")
    if len(selected) < len(all_events):
        lines.append('  truncated["轨迹截断"]')
    return "\n".join(lines)


def execution_waterfall(
    events: Iterable[TraceEvent], *, max_rows: int = 512,
) -> tuple[TraceWaterfallRow, ...]:
    """从历史事件聚合 bounded waterfall 行。"""
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
        raise ValueError("max_rows must be positive")
    states: dict[tuple[str, str | None], list[object]] = {}
    for event in events:
        if event.execution_id is None:
            continue
        key = (event.execution_id, event.attempt_id)
        state = states.setdefault(key, [event, None, "running"])
        if event.kind == "turn_started":
            state[0] = event
        elif event.kind in {"turn_completed", "turn_failed", "turn_suspended"}:
            state[1] = event
            state[2] = {
                "turn_completed": "completed",
                "turn_failed": "failed",
                "turn_suspended": "waiting_input",
            }[event.kind]
    rows: list[TraceWaterfallRow] = []
    for start, end, status in states.values():
        assert isinstance(start, TraceEvent)
        assert end is None or isinstance(end, TraceEvent)
        rows.append(TraceWaterfallRow(
            execution_id=start.execution_id or "",
            attempt_id=start.attempt_id,
            skill_id=start.skill_id,
            started_at=start.timestamp,
            finished_at=end.timestamp if end is not None else None,
            status=str(status),
        ))
    return tuple(rows[:max_rows])


def execution_tree_mermaid(
    events: Iterable[TraceEvent], *, max_nodes: int = 512,
) -> str:
    """从历史 execution identity 生成有界父子 Mermaid 树。"""
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes <= 0:
        raise ValueError("max_nodes must be positive")
    latest: dict[str, TraceEvent] = {}
    parents: dict[str, str | None] = {}
    for event in events:
        if event.execution_id is None:
            continue
        latest[event.execution_id] = event
        parents[event.execution_id] = event.parent_execution_id
        if len(latest) >= max_nodes:
            break
    lines = ["flowchart TD"]
    indexes = {execution_id: index for index, execution_id in enumerate(latest)}
    for execution_id, event in latest.items():
        index = indexes[execution_id]
        label = f"{event.skill_id or execution_id} [{execution_id}]"[:180]
        lines.append(f'  n{index}["{label.replace(chr(34), chr(39))}"]')
    for execution_id, parent_id in parents.items():
        if parent_id in indexes:
            lines.append(f"  n{indexes[parent_id]} --> n{indexes[execution_id]}")
    return "\n".join(lines)


__all__ = [
    "TraceWaterfallRow",
    "execution_tree_mermaid",
    "execution_waterfall",
    "load_event_jsonl",
    "mermaid_timeline",
    "timeline_events",
]
