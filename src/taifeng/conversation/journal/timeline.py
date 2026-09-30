"""Timeline 投影（session-journal，ADR 0104）。

Timeline 按 ``seq`` 把 Journal 的每一条记录映射成一个稳定的条目：领域记录是唯一权威表示，
这里不另造语义事件。每个条目回链 ``record_id`` / ``seq``，展示 ``recorded_at``；可按 thread、
turn、submission、actor、record type、call id、skill id 筛选；客户端断线后凭 ``after_seq``
补读，实时事件只是「新水位到了」的通知。

投影不写任何东西，也不改变事实源；脱敏见 ``redaction``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.conversation.journal.redaction import TimelineView, redact_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Sequence
    from datetime import datetime

    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.journal.redaction import RedactionEntry


class TimelineSource(Protocol):
    """Timeline 需要的 Journal 读能力：按序号 strict 读取已提交的记录。"""

    def load(
        self, session_id: str, *, after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """读 ``after_seq`` 之后的 committed envelopes。"""
        ...

_CALL_ID_KEYS = ("call_id", "parent_call_id", "related_call_id")
_SKILL_ID_KEYS = ("skill_id", "then_skill_id", "entry_skill_id", "target_skill_id")


@dataclass(frozen=True, slots=True)
class TimelineItem:
    """Timeline 的一个条目：一条 Journal 记录的稳定视图。

    ``payload`` 按视图给出：``full`` 是原 payload，``redacted`` 是占位后的 payload，
    ``metadata_only`` 为空；``redactions`` 只在 ``redacted`` 视图非空。
    """

    seq: int
    record_id: str
    record_type: str
    recorded_at: datetime
    occurred_at: datetime | None
    actor_kind: str
    actor_source: str
    thread_id: str | None
    submission_id: str | None
    turn_id: str | None
    operation_id: str | None
    causation_id: str | None
    call_id: str | None
    skill_id: str | None
    payload: dict[str, Any]
    payload_hash: str
    redactions: tuple[RedactionEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class TimelineFilter:
    """筛选条件；为空的维度不限制。"""

    thread_id: str | None = None
    turn_id: str | None = None
    submission_id: str | None = None
    actor_kind: str | None = None
    record_types: Collection[str] = field(default_factory=frozenset)
    call_id: str | None = None
    skill_id: str | None = None

    def matches(self, item: TimelineItem) -> bool:
        """条目是否落在筛选范围内。"""
        checks = (
            (self.thread_id, item.thread_id),
            (self.turn_id, item.turn_id),
            (self.submission_id, item.submission_id),
            (self.actor_kind, item.actor_kind),
            (self.call_id, item.call_id),
            (self.skill_id, item.skill_id),
        )
        if any(wanted is not None and wanted != actual for wanted, actual in checks):
            return False
        return not self.record_types or item.record_type in self.record_types


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """一次读取的结果：条目、已读到的水位、视图是否完整。

    ``last_seq`` 是本次扫过的最后一条记录的序号（含被筛掉的），下一次以它作 ``after_seq``。
    ``audit_complete`` 在 ``metadata_only`` 视图为 False。
    """

    items: tuple[TimelineItem, ...]
    last_seq: int
    view: TimelineView
    audit_complete: bool


def _first_str(payload: dict[str, Any], keys: Sequence[str]) -> str | None:
    """payload 顶层（或对话项的 payload 内）第一个命中的字符串字段。"""
    inner = payload.get("payload") if payload.get("item_kind") is not None else None
    for source in (payload, inner if isinstance(inner, dict) else {}):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def timeline_item(envelope: JournalEnvelope, view: TimelineView = "full") -> TimelineItem:
    """把一条记录映射为条目；``payload_hash`` 永远是原 payload 的 canonical hash。"""
    payload = dict(envelope.payload)
    redactions: tuple[RedactionEntry, ...] = ()
    if view == "redacted":
        redacted = redact_payload(payload)
        shown, redactions, payload_hash = (
            redacted.payload, redacted.manifest, redacted.original_payload_hash,
        )
    else:
        payload_hash = redact_payload(payload).original_payload_hash
        shown = payload if view == "full" else {}
    return TimelineItem(
        seq=envelope.seq,
        record_id=envelope.record_id,
        record_type=envelope.record_type,
        recorded_at=envelope.recorded_at,
        occurred_at=envelope.occurred_at,
        actor_kind=envelope.actor.kind,
        actor_source=envelope.actor.source,
        thread_id=envelope.thread_id,
        submission_id=envelope.submission_id,
        turn_id=envelope.turn_id,
        operation_id=envelope.operation_id,
        causation_id=envelope.causation_id,
        call_id=_first_str(payload, _CALL_ID_KEYS),
        skill_id=_first_str(payload, _SKILL_ID_KEYS),
        payload=shown,
        payload_hash=payload_hash,
        redactions=redactions,
    )


class JournalTimelineProjector:
    """从 Journal 读 Timeline；只读，不持有状态。"""

    def __init__(self, journal: TimelineSource) -> None:
        """
        Args:
            journal: 提供 ``load(session_id, after_seq=)`` 的 Journal core。
        """
        self._journal = journal

    async def read(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        view: TimelineView = "full",
        filter: TimelineFilter | None = None,  # noqa: A002
        limit: int | None = None,
    ) -> TimelinePage:
        """读 ``after_seq`` 之后的条目，按 ``seq`` 升序。

        ``limit`` 限制返回的条目数；到达上限时 ``last_seq`` 停在最后一个返回条目的序号，
        再次以它作 ``after_seq`` 继续，不会漏。

        Raises:
            ValueError: ``after_seq`` 为负或 ``limit`` 非正。
        """
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        items: list[TimelineItem] = []
        last_seq = after_seq
        async for envelope in self._journal.load(session_id, after_seq=after_seq):
            item = timeline_item(envelope, view)
            if filter is not None and not filter.matches(item):
                last_seq = envelope.seq
                continue
            items.append(item)
            last_seq = envelope.seq
            if limit is not None and len(items) >= limit:
                break
        return TimelinePage(
            items=tuple(items),
            last_seq=last_seq,
            view=view,
            audit_complete=view != "metadata_only",
        )


__all__ = [
    "JournalTimelineProjector",
    "TimelineFilter",
    "TimelineItem",
    "TimelinePage",
    "TimelineSource",
    "timeline_item",
]
