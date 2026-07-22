"""把已 durable ack 的 conversation_item 投影到可重建 MessageStore。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from pydantic import ValidationError

from taifeng.conversation.journal.records import (
    ConversationItemV1,
    deserialize_response_item,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from taifeng.conversation.journal.models import JournalAck, JournalEnvelope
    from taifeng.conversation.models import ResponseItem


class ProjectionOrderError(ValueError):
    """Journal 顺序、ack 覆盖或 conversation wire contract 不成立。"""


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """单 thread materialization 的可观察状态。"""

    thread_id: str
    projected_seq: int
    stale: bool
    failure_class: str | None = None
    failure_record_id: str | None = None


class ConversationProjectionStore(Protocol):
    """Journal projector 所需的最窄 transcript store 协议。"""

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """用调用方预分配 id 创建空投影。"""
        ...

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """按输入顺序追加 conversation items。"""
        ...

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """按 durable 写入顺序加载已投影 items。"""
        ...


@dataclass(frozen=True, slots=True)
class _ProjectedEnvelope:
    """已经完整验证、可安全物化的 envelope。"""

    seq: int
    record_id: str
    item: ResponseItem


def _validate_audit_metadata(extra: dict[str, Any]) -> None:
    """要求调用方显式提供完整 audited transcript marker。"""
    session_id = extra.get("journal_session_id")
    schema_version = extra.get("journal_schema_version")
    if (
        extra.get("audit_required") is not True
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ProjectionOrderError("complete audit metadata is required")


def _validate_envelope(envelope: JournalEnvelope, ack: JournalAck) -> _ProjectedEnvelope:
    """验证单条 envelope 受 ack 覆盖且 payload 是显式 ConversationItemV1。"""
    if envelope.record_type != "conversation_item":
        raise ProjectionOrderError("only conversation_item envelopes may be projected")
    if envelope.session_id != ack.session_id:
        raise ProjectionOrderError("envelope session does not match ack")
    if envelope.writer_epoch != ack.writer_epoch:
        raise ProjectionOrderError("envelope writer epoch does not match ack")
    if not ack.first_seq <= envelope.seq <= ack.last_seq:
        raise ProjectionOrderError("envelope seq is not covered by ack")
    if envelope.record_id not in ack.record_ids:
        raise ProjectionOrderError("envelope record id is not covered by ack")
    try:
        payload = ConversationItemV1.model_validate(envelope.payload)
        item = deserialize_response_item(payload)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ProjectionOrderError("invalid ConversationItemV1 payload") from exc
    if envelope.thread_id != payload.thread_id or item.thread_id != payload.thread_id:
        raise ProjectionOrderError("envelope and item thread ids do not match")
    return _ProjectedEnvelope(seq=envelope.seq, record_id=envelope.record_id, item=item)


def _validate_batch(
    envelopes: Sequence[JournalEnvelope], ack: JournalAck
) -> tuple[_ProjectedEnvelope, ...]:
    """在任何 store write 前验证完整单 thread batch。"""
    if not envelopes:
        raise ProjectionOrderError("projection batch must not be empty")
    if ack.first_seq > ack.last_seq:
        raise ProjectionOrderError("ack sequence range is invalid")
    expected_record_count = ack.last_seq - ack.first_seq + 1
    if len(ack.record_ids) != expected_record_count:
        raise ProjectionOrderError("ack record ids must cover its complete sequence range")
    if len(set(ack.record_ids)) != len(ack.record_ids):
        raise ProjectionOrderError("ack record ids must be unique")
    projected: list[_ProjectedEnvelope] = []
    seen_records: set[str] = set()
    seen_items: set[str] = set()
    previous_seq = 0
    for envelope in envelopes:
        if envelope.seq <= previous_seq:
            raise ProjectionOrderError("envelope seq must be strictly increasing")
        if envelope.record_id in seen_records:
            raise ProjectionOrderError("envelope record ids must be unique")
        entry = _validate_envelope(envelope, ack)
        ack_offset = envelope.seq - ack.first_seq
        if ack.record_ids[ack_offset] != envelope.record_id:
            raise ProjectionOrderError("envelope order does not match ack record order")
        if entry.item.id in seen_items:
            raise ProjectionOrderError("conversation item ids must be unique")
        projected.append(entry)
        previous_seq = envelope.seq
        seen_records.add(envelope.record_id)
        seen_items.add(entry.item.id)
    thread_ids = {entry.item.thread_id for entry in projected}
    if len(thread_ids) != 1:
        raise ProjectionOrderError("cross-thread projection batches are not supported")
    return tuple(projected)


class JournalConversationProjector:
    """只把 covering durable JournalAck 对应的 conversation envelopes 投影到 store。

    每次调用只接受一个 thread；ack 可覆盖同 batch 中未传入的领域 records，因此 seq gap 合法。
    watermark 是 projector 实例内的 per-thread 状态，重启后通过 Journal 顺序重放恢复；item 去重则
    每次读取 durable transcript，以便新 projector 或部分 append 失败后仍可收敛。
    """

    def __init__(self, store: ConversationProjectionStore) -> None:
        self._store = store
        self._states: dict[str, ProjectionResult] = {}
        self._locks: dict[str, anyio.Lock] = {}

    async def bootstrap_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """校验调用方审计 marker 后创建预分配 id 的空投影。"""
        _validate_audit_metadata(extra)
        return await self._store.create_projection_thread(
            thread_id=thread_id,
            cwd=cwd,
            entry_skill_id=entry_skill_id,
            source=source,
            extra=extra,
        )

    def state(self, thread_id: str) -> ProjectionResult:
        """返回 thread 当前 watermark/stale 快照。"""
        return self._states.get(
            thread_id,
            ProjectionResult(thread_id=thread_id, projected_seq=0, stale=False),
        )

    async def project(
        self,
        envelopes: Sequence[JournalEnvelope],
        ack: JournalAck,
    ) -> ProjectionResult:
        """验证完整 batch 后按 Journal seq 物化；store 失败只返回 stale。"""
        projected = _validate_batch(envelopes, ack)
        thread_id = projected[0].item.thread_id
        lock = self._locks.setdefault(thread_id, anyio.Lock())
        async with lock:
            return await self._materialize(thread_id, projected)

    async def _materialize(
        self,
        thread_id: str,
        projected: tuple[_ProjectedEnvelope, ...],
    ) -> ProjectionResult:
        """在 per-thread 锁内核对 durable history、去重并追加缺失 suffix。"""
        current = self.state(thread_id)
        first_missing = 0
        try:
            iterator = await self._store.load_thread(thread_id)
            history = [item async for item in iterator]
            existing = {item.id: item for item in history}
            if len(existing) != len(history):
                return self._set_stale(
                    thread_id,
                    current.projected_seq,
                    "duplicate_stored_item_id",
                    projected[0].record_id,
                )
            for entry in projected:
                stored = existing.get(entry.item.id)
                if stored is not None and stored != entry.item:
                    return self._set_stale(
                        thread_id, current.projected_seq, "item_id_conflict", entry.record_id
                    )
            present = [entry.item.id in existing for entry in projected]
            first_missing = next(
                (index for index, value in enumerate(present) if not value),
                len(present),
            )
            if any(present[first_missing:]):
                return self._set_stale(
                    thread_id,
                    current.projected_seq,
                    "projection_order_conflict",
                    projected[0].record_id,
                )
            if (
                history
                and first_missing == 0
                and projected[-1].seq < current.projected_seq
            ):
                return self._set_stale(
                    thread_id,
                    current.projected_seq,
                    "projection_order_conflict",
                    projected[0].record_id,
                )
            items = [entry.item for entry in projected[first_missing:]]
            if items:
                await self._store.append_batch(items)
        except Exception as exc:  # noqa: BLE001  # materialization 不得冻结 Journal
            return self._set_stale(
                thread_id,
                current.projected_seq,
                type(exc).__name__,
                projected[first_missing].record_id if first_missing < len(projected) else None,
            )
        healthy = ProjectionResult(
            thread_id=thread_id,
            projected_seq=max(current.projected_seq, projected[-1].seq),
            stale=False,
        )
        self._states[thread_id] = healthy
        return healthy

    def _set_stale(
        self,
        thread_id: str,
        projected_seq: int,
        failure_class: str,
        failure_record_id: str | None,
    ) -> ProjectionResult:
        """保存并返回稳定的 materialization stale 分类。"""
        stale = ProjectionResult(
            thread_id=thread_id,
            projected_seq=projected_seq,
            stale=True,
            failure_class=failure_class,
            failure_record_id=failure_record_id,
        )
        self._states[thread_id] = stale
        return stale
