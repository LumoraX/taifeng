"""把 JSONL 和 Journal Timeline 转成可回放的轻量轨迹。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from taifeng.telemetry.trace_projection import TraceEvent, TraceProjection, TraceSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterable


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
        label = f"{event.seq} {event.kind} [{event.execution_id or '-'}]"
        safe = label.replace('"', "'").replace("\n", " ")[:180]
        lines.append(f'  e{index}["{safe}"]')
        if index:
            lines.append(f"  e{index - 1} --> e{index}")
        for key, value in event.metadata:
            if key == "operation_id":
                record_nodes[value] = f"e{index}"
    for index, event in enumerate(selected):
        for key, value in event.metadata:
            if key == "causation_id" and value in record_nodes:
                lines.append(f"  {record_nodes[value]} -. causation .-> e{index}")
    if len(selected) < len(all_events):
        lines.append('  truncated["轨迹截断"]')
    return "\n".join(lines)


__all__ = ["load_event_jsonl", "mermaid_timeline", "timeline_events"]
