"""从 Journal 重建对话投影（session-journal，ADR 0104）。

投影（每个 thread 一份 JSONL transcript）是 Journal 的可重建派生物：删掉全部物化数据后可以从
Journal 原样重建，接管的结果不变。重建逐 thread 进行——``thread_created`` 记录说明有哪些 thread、
它们的入口 skill 与来源，``conversation_item`` 记录按序号给出内容。

已有投影是 Journal 内容的前缀时补齐后缀；分叉的投影不改写，记在结果里由调用方删除后重跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    ThreadCreatedV1,
    deserialize_response_item,
)

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.journal.timeline import TimelineSource
    from taifeng.conversation.transcript import JsonlMessageStore


@dataclass(frozen=True, slots=True)
class ThreadRebuild:
    """一个 thread 的重建结果。"""

    thread_id: str
    item_count: int
    created: bool
    status: str
    """``rebuilt``（已与 Journal 一致）/ ``divergent``（已有投影与 Journal 分叉，未改写）。"""


@dataclass(frozen=True, slots=True)
class ProjectionRebuildResult:
    """一次重建的全部结果。"""

    session_id: str
    threads: tuple[ThreadRebuild, ...] = field(default_factory=tuple)

    @property
    def divergent(self) -> tuple[str, ...]:
        """与 Journal 分叉、需要删除后重跑的 thread。"""
        return tuple(t.thread_id for t in self.threads if t.status == "divergent")


@dataclass(slots=True)
class _ThreadFacts:
    """重建一个 thread 所需的事实。"""

    thread_id: str
    entry_skill_id: str
    source: str
    extra: dict[str, Any]
    items: list[Any] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)


def _thread_facts(envelopes: list[JournalEnvelope]) -> list[_ThreadFacts]:
    """按创建顺序收集每个 thread 的元数据与对话项。"""
    facts: dict[str, _ThreadFacts] = {}
    for envelope in envelopes:
        thread_id = envelope.thread_id
        if envelope.record_type == "thread_created" and thread_id is not None:
            payload = envelope.payload
            if envelope.seq == 2:
                # 初始化批次的 root thread（V0 形状：直接取描述符字段）
                raw_extra = payload.get("extra")
                facts[thread_id] = _ThreadFacts(
                    thread_id, str(payload.get("entry_skill_id") or "general"),
                    str(payload.get("source") or "user"),
                    dict(raw_extra) if isinstance(raw_extra, dict) else {},
                )
            else:
                created = ThreadCreatedV1.model_validate(payload)
                facts[thread_id] = _ThreadFacts(
                    thread_id, created.entry_skill_id, created.source, dict(created.extra),
                )
        elif envelope.record_type == "conversation_item" and thread_id in facts:
            item = deserialize_response_item(ConversationItemV1.model_validate(envelope.payload))
            facts[thread_id].items.append(item)
            facts[thread_id].seqs.append(envelope.seq)
    return list(facts.values())


async def rebuild_projections(
    *,
    journal: TimelineSource,
    store: JsonlMessageStore,
    session_id: str,
) -> ProjectionRebuildResult:
    """按 Journal 重建该 Session 全部 thread 的投影。

    Raises:
        ValueError: Journal 里没有这个 Session，或形状违约。
    """
    envelopes = [envelope async for envelope in journal.load(session_id)]
    if not envelopes:
        raise ValueError(f"journal session not found: {session_id}")
    projector = JournalConversationProjector(store)
    results: list[ThreadRebuild] = []
    for facts in _thread_facts(envelopes):
        created = await store.audited_projection_marker(facts.thread_id) is None
        if created:
            extra = {
                key: value for key, value in facts.extra.items() if key != "cwd"
            }
            await projector.bootstrap_thread(
                thread_id=facts.thread_id,
                cwd=facts.extra.get("cwd") if isinstance(facts.extra.get("cwd"), str) else None,
                entry_skill_id=facts.entry_skill_id,
                source=facts.source,
                extra={
                    "audit_required": True,
                    "journal_session_id": session_id,
                    "journal_schema_version": 1,
                    **extra,
                },
            )
        outcome = await projector.reconcile_resumed_thread(
            thread_id=facts.thread_id,
            session_id=session_id,
            items=facts.items,
            first_seq=facts.seqs[0] if facts.seqs else None,
            last_seq=facts.seqs[-1] if facts.seqs else envelopes[-1].seq,
        )
        results.append(ThreadRebuild(
            thread_id=facts.thread_id,
            item_count=len(facts.items),
            created=created,
            status="divergent" if outcome.stale else "rebuilt",
        ))
    return ProjectionRebuildResult(session_id, tuple(results))


__all__ = ["ProjectionRebuildResult", "ThreadRebuild", "rebuild_projections"]
