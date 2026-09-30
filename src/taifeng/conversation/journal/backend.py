"""SessionJournal 后端 seam：core 协议与存储无关的构件（ADR 0114）。

写一个新的 Journal 后端有两条路，代价不同：

1. **换存储，不换 core**。``JsonlSessionJournalCore`` 把物理读写收在 ``SyncFileAdapter``（独占创建 /
   整读 / durable 追加三个同步方法），把跨进程互斥收在 ``WriterLockAdapter``（获取 / 释放）。实现这
   五个方法即可把 Journal 放到共享存储上，hash chain、batch 帧、幂等、接管、strict verify 全部沿用
   内核的实现。代价：core 每次追加都整读一遍该 Session 的 Journal 做 strict scan。
2. **换 core**。实现 ``SessionJournalCore`` 的五个方法，存储形态自定（一行一条 record 的数据库表等），
   追加可以做到与 Journal 长度无关。本模块给出不依赖存储的构件：``seal_batch``（分配 seq、算 hash
   chain）、``resolve_idempotent_ack``（重复提交的判定）、``verify_envelopes``（整条链的 strict 校验），
   加上 ``models`` 里的 ``build_initialization_records`` / ``build_takeover_record``。
   ``InMemorySessionJournalCore``（``journal.memory``）是照这条路写的参考实现。

两条路都用同一套一致性检查验收：``taifeng.testing.journal_conformance``。

本模块只有纯函数与协议，不做任何 IO。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng.conversation.journal.canonical import (
    canonical_hash,
    model_canonical_data,
    record_fingerprint,
)
from taifeng.conversation.journal.errors import JournalConflictError, JournalIntegrityError
from taifeng.conversation.journal.framing import (
    _check_envelope_epoch,
    _envelope_for_record,
    _validate_envelope,
)
from taifeng.conversation.journal.models import (
    SESSION_ENDED_RECORD_TYPE,
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from taifeng.conversation.journal.models import (
        SessionCreateResult,
        SessionDescriptor,
        SessionLease,
        SessionOpenResult,
    )

ZERO_HASH = "0" * 64
"""Session 第一条 record 的 ``previous_hash``。"""


@runtime_checkable
class SessionJournalCore(Protocol):
    """审计模式依赖的 Journal core 边界（``AuditConfig.journal_core``）。

    语义以 ``session-journal-core`` 契约为准，``taifeng.testing.journal_conformance`` 逐条检查：

    - 一个 Session 同一时刻只有一个 writer；``create_session`` / ``open_existing`` 取得 writer 并返回
      lease，``close_session`` 释放。别的实例在 writer 存活时只会得到 ``JournalBusyError``。
    - ``append_batch`` 整批原子、按 ``expected_seq`` 做 CAS；完整重复的提交返回原 ack（幂等），内容
      不同或部分重叠抛 ``JournalConflictError``；lease 不符抛 ``JournalLeaseError``。
    - ``open_existing`` 以 ``writer_epoch + 1`` 接管，并写下一条 ``writer_takeover`` 记录。
    - ``load`` 只给已提交的 envelope，按 seq 升序。
    """

    async def create_session(self, descriptor: SessionDescriptor) -> SessionCreateResult:
        """原子创建一个新 Session Journal（初始化三记录，seq 1–3，epoch 1）。"""
        ...

    async def open_existing(
        self,
        session_id: str,
        *,
        writer_id: str,
        operation_id: str,
    ) -> SessionOpenResult:
        """以更高 writer epoch 接管已有 Session（resume 用）。"""
        ...

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """按 expected seq 追加一个 durable batch。"""
        ...

    async def close_session(self, lease: SessionLease) -> None:
        """只释放指定 Session 的 writer lease。"""
        ...

    def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """strict 读取 durable committed envelopes。"""
        ...


@dataclass(frozen=True, slots=True)
class CommittedRecord:
    """幂等索引里的一条：调用方 record 的 fingerprint 与它所在原 batch 的 ack。"""

    fingerprint: str
    ack: JournalAck


@dataclass(frozen=True, slots=True)
class SealedBatch:
    """一批已分配 seq、算好 hash chain、尚待落库的 envelope。"""

    envelopes: tuple[JournalEnvelope, ...]
    fingerprints: tuple[str, ...]
    ack: JournalAck

    def committed(self) -> dict[str, CommittedRecord]:
        """落库成功后并入幂等索引的条目：record id → (fingerprint, 本批 ack)。"""
        return {
            envelope.record_id: CommittedRecord(fingerprint, self.ack)
            for envelope, fingerprint in zip(self.envelopes, self.fingerprints, strict=True)
        }


def descriptor_fingerprint(descriptor: SessionDescriptor) -> str:
    """创建请求的 fingerprint：同一 live writer 重试同一次创建时据此判定「是同一次」。"""
    return canonical_hash(model_canonical_data(descriptor))


def snapshot_records(
    records: Sequence[JournalRecord],
) -> tuple[tuple[JournalRecord, ...], tuple[str, ...]]:
    """在任何 await 之前固定调用方输入：重新校验成独立副本，并只算一次 fingerprint。

    调用方可能在 core 等 IO 期间改动自己手里的 DTO；后续编码、幂等比较都必须用这份快照。

    Raises:
        ValueError: 空批次，或批内 record 不属于同一个 Session。
    """
    if not records:
        raise ValueError("journal batch must contain at least one record")
    snapshots = tuple(
        JournalRecord.model_validate(record.model_dump(mode="python")) for record in records
    )
    session_id = snapshots[0].session_id
    if any(record.session_id != session_id for record in snapshots):
        raise ValueError("all records in a batch must belong to one session")
    return snapshots, tuple(record_fingerprint(record) for record in snapshots)


def seal_batch(
    records: Sequence[JournalRecord],
    *,
    previous_seq: int,
    previous_hash: str,
    writer_epoch: int,
    recorded_at: datetime | None = None,
    fingerprints: tuple[str, ...] | None = None,
) -> SealedBatch:
    """给一批 record 分配连续 seq 并接上 hash chain。

    Args:
        previous_seq: 当前已提交的尾 seq（空 Session 为 0）。
        previous_hash: 当前尾 envelope 的 ``record_hash``（空 Session 为 ``ZERO_HASH``）。
        writer_epoch: 本批的 writer epoch（整批相同）。
        recorded_at: 落账时间；不给取当前 UTC 时间。
        fingerprints: ``snapshot_records`` 算好的 fingerprint；不给则现算。

    Raises:
        ValueError: 空批次、跨 Session、批内 record id 重复、fingerprint 数量不符。
    """
    if not records:
        raise ValueError("journal batch must contain at least one record")
    session_id = records[0].session_id
    if any(record.session_id != session_id for record in records):
        raise ValueError("all records in a batch must belong to one session")
    if len({record.record_id for record in records}) != len(records):
        raise ValueError("record ids must be unique within a new batch")
    resolved = (
        tuple(record_fingerprint(record) for record in records)
        if fingerprints is None else fingerprints
    )
    if len(resolved) != len(records):
        raise ValueError("record fingerprints must match record count")
    stamp = recorded_at or datetime.now(UTC)
    envelopes: list[JournalEnvelope] = []
    tail_hash = previous_hash
    for offset, record in enumerate(records, start=1):
        envelope = _envelope_for_record(
            record, seq=previous_seq + offset, writer_epoch=writer_epoch,
            previous_hash=tail_hash, recorded_at=stamp,
        )
        envelopes.append(envelope)
        tail_hash = envelope.record_hash
    ack = JournalAck(
        session_id=session_id,
        first_seq=envelopes[0].seq,
        last_seq=envelopes[-1].seq,
        record_ids=tuple(record.record_id for record in records),
        tail_hash=tail_hash,
        writer_epoch=writer_epoch,
        durability=Durability.COMMITTED,
    )
    return SealedBatch(tuple(envelopes), resolved, ack)


def resolve_idempotent_ack(
    records: Sequence[JournalRecord],
    fingerprints: Sequence[str],
    lookup: Callable[[str], CommittedRecord | None],
) -> JournalAck | None:
    """判定一次提交是不是此前某个 batch 的完整重试（在 CAS 之前调用）。

    Returns:
        原 batch 的 ack（重试命中）；None 表示这是新内容，接下来按 ``expected_seq`` 做 CAS。

    Raises:
        JournalConflictError: 同 record id 内容不同，或只与已提交内容部分重叠 / 跨 batch 重组。
    """
    indexed = tuple(lookup(record.record_id) for record in records)
    if not any(item is not None for item in indexed):
        return None
    if not all(item is not None for item in indexed):
        raise JournalConflictError("batch idempotency conflict")
    committed = tuple(item for item in indexed if item is not None)
    if any(item.fingerprint != fp for item, fp in zip(committed, fingerprints, strict=True)):
        if len(records) == 1:
            raise JournalConflictError("record content conflict", record_id=records[0].record_id)
        raise JournalConflictError("batch idempotency conflict")
    ack = committed[0].ack
    if any(item.ack != ack for item in committed) or ack.record_ids != tuple(
        record.record_id for record in records
    ):
        raise JournalConflictError("batch idempotency conflict")
    return ack


def verify_envelopes(
    envelopes: Sequence[JournalEnvelope], *, session_id: str,
) -> JournalVerification:
    """从 seq 1 起 strict 校验一条完整的已提交链。

    检查：Session 归属、seq 连续、``previous_hash`` 接续、``payload_hash`` / ``record_hash`` 可复算、
    writer epoch 从 1 起且只经 ``writer_takeover`` 单步递增（其 payload 精确指向接管前的尾）。

    Raises:
        JournalIntegrityError: 任一条违约（``line_no`` 是出问题的 envelope 的序号，从 1 起）。
    """
    tail_hash = ZERO_HASH
    epoch = 0
    for position, envelope in enumerate(envelopes, start=1):
        _validate_envelope(
            envelope, session_id=session_id, expected_seq=position,
            expected_previous_hash=tail_hash, line_no=position,
        )
        _check_envelope_epoch(envelope, batch_epoch=None, committed_epoch=epoch, line_no=position)
        tail_hash = envelope.record_hash
        epoch = envelope.writer_epoch
    return JournalVerification(
        session_id=session_id,
        health=JournalHealth.HEALTHY,
        committed_tail_seq=len(envelopes),
        committed_tail_hash=tail_hash,
        record_count=len(envelopes),
    )


def ended_record_id(envelopes: Sequence[JournalEnvelope]) -> str | None:
    """已提交的 ``session_ended`` 的 record id；Session 还没终结返回 None。"""
    for envelope in envelopes:
        if envelope.record_type == SESSION_ENDED_RECORD_TYPE:
            return envelope.record_id
    return None


def index_envelopes(
    envelopes: Sequence[JournalEnvelope], batches: Sequence[JournalAck],
) -> dict[str, CommittedRecord]:
    """从已提交的 envelope 与各 batch 的 ack 重建幂等索引（接管后的新 writer 要用）。

    Raises:
        JournalIntegrityError: 有 envelope 不属于任何给出的 batch。
    """
    by_seq = {seq: ack for ack in batches for seq in range(ack.first_seq, ack.last_seq + 1)}
    index: dict[str, CommittedRecord] = {}
    for envelope in envelopes:
        ack = by_seq.get(envelope.seq)
        if ack is None:
            raise JournalIntegrityError("envelope outside any committed batch")
        record = JournalRecord.model_validate({
            key: value for key, value in envelope.model_dump(mode="python").items()
            if key in JournalRecord.model_fields
        })
        index[envelope.record_id] = CommittedRecord(record_fingerprint(record), ack)
    return index


__all__ = [
    "ZERO_HASH",
    "CommittedRecord",
    "SealedBatch",
    "SessionJournalCore",
    "descriptor_fingerprint",
    "ended_record_id",
    "index_envelopes",
    "resolve_idempotent_ack",
    "seal_batch",
    "snapshot_records",
    "verify_envelopes",
]
