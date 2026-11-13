"""全局事件轨迹的有界轻量投影。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from taifeng.loop.event import EventMsg

_SENSITIVE_KEYS = frozenset({"arguments", "output", "delta", "summary", "prompt"})


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """一条不携带正文的全局事件轨迹记录。"""

    seq: int
    timestamp: datetime
    kind: str
    submission_id: str
    execution_id: str | None
    attempt_id: str | None
    thread_id: str | None
    skill_id: str | None
    parent_execution_id: str | None
    parent_thread_id: str | None
    call_id: str | None
    metadata: tuple[tuple[str, str | int | float | bool], ...]


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    """全局有序轨迹及其完整性状态。"""

    events: tuple[TraceEvent, ...]
    incomplete: bool


def _scalar_metadata(
    data: dict[str, Any], *, max_value_length: int, max_items: int,
) -> tuple[tuple[str, str | int | float | bool], ...]:
    """过滤正文和复杂对象，保留有限的标量事实。"""
    result: list[tuple[str, str | int | float | bool]] = []
    for key in sorted(data):
        if key in _SENSITIVE_KEYS or len(result) >= max_items:
            continue
        value = data[key]
        if isinstance(value, str | bool | int | float):
            if isinstance(value, str):
                value = value[:max_value_length]
            result.append((key, value))
    return tuple(result)


class TraceProjection:
    """按全局 seq 保存轻量事件历史，不推测调用关系。"""

    def __init__(
        self,
        max_events: int = 8192,
        *,
        max_value_length: int = 256,
        max_metadata_items: int = 16,
    ) -> None:
        """初始化有界投影。"""
        if max_events <= 0 or max_value_length <= 0 or max_metadata_items <= 0:
            raise ValueError("trace limits must be positive")
        self.max_events = max_events
        self.max_value_length = max_value_length
        self.max_metadata_items = max_metadata_items
        self.incomplete = False
        self._events: list[TraceEvent] = []
        self._seq = -1

    def snapshot(self) -> TraceSnapshot:
        """返回不可变全局轨迹快照。"""
        return TraceSnapshot(tuple(self._events), self.incomplete)

    def handle(self, ev: EventMsg) -> None:
        """按 seq 追加事件；重复、迟到、跳号或超容量均显式暴露。"""
        if ev.seq <= self._seq:
            self.incomplete |= ev.seq < self._seq
            return
        if ev.seq != self._seq + 1:
            self.incomplete = True
        self._seq = ev.seq
        if len(self._events) >= self.max_events:
            self.incomplete = True
            return
        identity = ev.execution
        self._events.append(TraceEvent(
            seq=ev.seq,
            timestamp=ev.timestamp,
            kind=ev.msg.kind,
            submission_id=ev.submission_id,
            execution_id=identity.execution_id if identity else None,
            attempt_id=identity.attempt_id if identity else None,
            thread_id=identity.thread_id if identity else None,
            skill_id=identity.skill_id if identity else None,
            parent_execution_id=identity.parent_execution_id if identity else None,
            parent_thread_id=identity.parent_thread_id if identity else None,
            call_id=(ev.msg.data.get("call_id")
                     if isinstance(ev.msg.data.get("call_id"), str) else None),
            metadata=_scalar_metadata(
                ev.msg.data,
                max_value_length=self.max_value_length,
                max_items=self.max_metadata_items,
            ),
        ))

    def render_text(self, max_rows: int = 40) -> str:
        """渲染全局事件顺序；不把它伪装成树或 span。"""
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        rows = [
            f"全局轨迹 | {len(self._events)} 事件"
            + (" | 观测不完整" if self.incomplete else "")
        ]
        for event in self._events[: max_rows - 1]:
            identity = event.execution_id or "-"
            details = " ".join(f"{key}={value}" for key, value in event.metadata)
            rows.append(f"{event.seq:04d} {event.kind} [{identity}] {details}".rstrip())
        if len(self._events) >= max_rows:
            rows.append("… 全局轨迹输出截断")
        return "\n".join(rows[:max_rows])


__all__ = ["TraceEvent", "TraceProjection", "TraceSnapshot"]
