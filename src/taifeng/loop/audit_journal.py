"""Session audit 的 Journal append protocol、receipt 与纯验证 helper。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from taifeng.conversation.journal.models import (
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalRecord,
    SessionLease,
)
from taifeng.loop.audit_support import _InvalidJournalAckError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

_HASH_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class JournalAppendCore(Protocol):
    """协调器依赖的最小 Journal append/load/close 边界。"""

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """以 caller expected seq 原子追加一个 batch。"""

    async def close_session(self, lease: SessionLease) -> None:
        """释放且只释放 coordinator 绑定的 per-Session writer。"""

    def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """strict 读取已 durable committed envelopes。"""


@dataclass(frozen=True, slots=True)
class JournalAppendReceipt:
    """协调器裁定的新提交或 historical durable receipt。"""

    ack: JournalAck
    newly_committed: bool

    @property
    def historical(self) -> bool:
        """返回本次 ack 是否只证明此前已提交的同一批 records。"""
        return not self.newly_committed


def validate_records(
    records: Sequence[JournalRecord],
    *,
    session_id: str,
) -> tuple[JournalRecord, ...]:
    """在 dispatch 前拒绝空、非 DTO 或跨 Session batch。"""
    snapshot = tuple(records)
    if not snapshot:
        raise ValueError("journal batch must contain at least one record")
    if any(not isinstance(record, JournalRecord) for record in snapshot):
        raise TypeError("journal batch accepts only JournalRecord values")
    if any(record.session_id != session_id for record in snapshot):
        raise ValueError("all records must belong to the same Session")
    return snapshot


def validate_ack(
    ack: object,
    records: tuple[JournalRecord, ...],
    *,
    session_id: str,
    writer_epoch: int,
    expected_seq: int,
) -> JournalAck:
    """以 exact type/字段/序列重验 ack，并返回防别名副本。"""
    if type(ack) is not JournalAck:
        raise _InvalidJournalAckError
    assert isinstance(ack, JournalAck)
    if not _ack_fields_are_exact(ack):
        raise _InvalidJournalAckError
    expected_record_ids = tuple(record.record_id for record in records)
    contiguous = ack.last_seq - ack.first_seq + 1 == len(records)
    advances_tail = (
        ack.first_seq == expected_seq + 1
        and ack.last_seq == expected_seq + len(records)
    )
    historical_retry = ack.last_seq <= expected_seq
    if not (
        ack.session_id == session_id
        and ack.writer_epoch == writer_epoch
        and ack.durability is Durability.COMMITTED
        and ack.record_ids == expected_record_ids
        and contiguous
        and (advances_tail or historical_retry)
    ):
        raise _InvalidJournalAckError
    return JournalAck.model_validate(ack.model_dump(mode="python"))


def _ack_fields_are_exact(ack: JournalAck) -> bool:
    """拒绝 bool/coercion/错误 hash 与非 tuple record ids。"""
    return (
        type(ack.session_id) is str
        and type(ack.first_seq) is int
        and type(ack.last_seq) is int
        and type(ack.record_ids) is tuple
        and all(type(record_id) is str and record_id for record_id in ack.record_ids)
        and type(ack.tail_hash) is str
        and _HASH_HEX_PATTERN.fullmatch(ack.tail_hash) is not None
        and type(ack.writer_epoch) is int
        and type(ack.durability) is Durability
    )


def validate_acknowledged_envelopes(
    loaded: tuple[object, ...],
    *,
    ack: JournalAck,
    records: tuple[JournalRecord, ...],
) -> tuple[JournalEnvelope, ...]:
    """重建并 exact 比对 covering ack、完整 records 与 envelope identity。"""
    envelopes = tuple(
        JournalEnvelope.model_validate(envelope.model_dump(mode="python"))
        for envelope in loaded
        if type(envelope) is JournalEnvelope
    )
    valid = (
        len(envelopes) == len(loaded)
        and len(envelopes) == len(ack.record_ids)
        and len(envelopes) == len(records)
        and tuple(envelope.seq for envelope in envelopes)
        == tuple(range(ack.first_seq, ack.last_seq + 1))
        and tuple(envelope.record_id for envelope in envelopes) == ack.record_ids
        and ack.tail_hash == envelopes[-1].record_hash
        and all(
            envelope.session_id == ack.session_id
            and envelope.writer_epoch == ack.writer_epoch
            for envelope in envelopes
        )
        and all(
            _envelope_matches_record(envelope, record)
            for envelope, record in zip(envelopes, records, strict=True)
        )
    )
    if not valid:
        raise _InvalidJournalAckError
    return envelopes


def _envelope_matches_record(
    envelope: JournalEnvelope,
    record: JournalRecord,
) -> bool:
    """比较 writer 之外的全部 JournalRecord 字段。"""
    restored = JournalRecord.model_validate({
        field: getattr(envelope, field)
        for field in JournalRecord.model_fields
    })
    return restored == record


__all__ = [
    "JournalAppendCore",
    "JournalAppendReceipt",
    "validate_ack",
    "validate_acknowledged_envelopes",
    "validate_records",
]
