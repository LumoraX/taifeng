"""内存里的 SessionJournal core：``SessionJournalCore`` 的参考实现（ADR 0114）。

两个用途：

- **给后端作者当样板**。它只用 ``journal.backend`` 与 ``journal.models`` 的公开构件写成，没有碰
  JSONL core 的任何内部——数据库版的后端照这个形状写：一张「Session」表（当前 writer、幂等索引）加
  一张「envelope」表，``create_session`` / ``append_batch`` / ``open_existing`` 各是一个事务。
- **给测试用**。审计模式的测试不想落文件时注入它。

``InMemoryJournalStorage`` 代表那台「数据库」：多个 core 实例接在同一份存储上，相当于多个进程。
进程内的数据，不 durable——进程没了 Journal 也没了，不能用于生产。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio

from taifeng.conversation.journal.backend import (
    ZERO_HASH,
    CommittedRecord,
    descriptor_fingerprint,
    ended_record_id,
    resolve_idempotent_ack,
    seal_batch,
    snapshot_records,
    verify_envelopes,
)
from taifeng.conversation.journal.canonical import canonical_hash
from taifeng.conversation.journal.errors import (
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalLeaseError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
)
from taifeng.conversation.journal.models import (
    WRITER_TAKEOVER_RECORD_TYPE,
    JournalAck,
    JournalEnvelope,
    JournalRecord,
    JournalVerification,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    SessionOpenResult,
    build_initialization_records,
    build_takeover_record,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _Holder:
    """一个 Session 当前的 writer：哪个 core 实例、凭哪次操作取得、返回过什么。"""

    owner: object
    operation_kind: str
    operation_id: str
    fingerprint: str
    result: SessionCreateResult | SessionOpenResult

    @property
    def lease(self) -> SessionLease:
        return self.result.lease


@dataclass
class _StoredSession:
    """存储里的一个 Session：已提交的 envelope、幂等索引、当前 writer。"""

    envelopes: list[JournalEnvelope] = field(default_factory=list)
    index: dict[str, CommittedRecord] = field(default_factory=dict)
    holder: _Holder | None = None

    @property
    def tail_seq(self) -> int:
        return len(self.envelopes)

    @property
    def tail_hash(self) -> str:
        return self.envelopes[-1].record_hash if self.envelopes else ZERO_HASH


class InMemoryJournalStorage:
    """多个 ``InMemorySessionJournalCore`` 共享的存储。"""

    def __init__(self) -> None:
        self.sessions: dict[str, _StoredSession] = {}
        # 一把存储级的锁：每个方法的「读状态 → 判定 → 写入」是一个事务
        self.lock = anyio.Lock()


class InMemorySessionJournalCore:
    """接在一份 ``InMemoryJournalStorage`` 上的 core 实例。"""

    def __init__(self, storage: InMemoryJournalStorage | None = None) -> None:
        """不给存储时自建一份（只有这一个实例能看到）。"""
        self._storage = storage if storage is not None else InMemoryJournalStorage()

    async def create_session(self, descriptor: SessionDescriptor) -> SessionCreateResult:
        """创建 Session 并提交初始化三记录。

        Raises:
            JournalBusyError: 该 Session 的 writer 还活着（不是同一次创建的重试）。
            JournalAlreadyExistsError: Session 已存在且没有 live writer。
        """
        descriptor = SessionDescriptor.model_validate(descriptor.model_dump(mode="python"))
        fingerprint = descriptor_fingerprint(descriptor)
        session_id = descriptor.session_id
        async with self._storage.lock:
            stored = self._storage.sessions.get(session_id)
            if stored is not None:
                holder = stored.holder
                if holder is None:
                    raise JournalAlreadyExistsError(session_id)
                if (
                    holder.owner is self
                    and holder.operation_kind == "create"
                    and holder.operation_id == descriptor.creation_operation_id
                    and holder.lease.writer_id == descriptor.writer_id
                    and holder.fingerprint == fingerprint
                    and isinstance(holder.result, SessionCreateResult)
                ):
                    return holder.result
                raise JournalBusyError(
                    session_id, holder.lease.writer_id if holder.owner is self else None)
            sealed = seal_batch(
                build_initialization_records(descriptor),
                previous_seq=0, previous_hash=ZERO_HASH, writer_epoch=1,
            )
            result = SessionCreateResult(
                lease=SessionLease(
                    session_id=session_id, writer_id=descriptor.writer_id,
                    writer_epoch=1, lease_id=uuid4().hex,
                ),
                ack=sealed.ack,
            )
            self._storage.sessions[session_id] = _StoredSession(
                envelopes=list(sealed.envelopes), index=sealed.committed(),
                holder=_Holder(self, "create", descriptor.creation_operation_id, fingerprint, result),
            )
            return result

    async def open_existing(
        self, session_id: str, *, writer_id: str, operation_id: str,
    ) -> SessionOpenResult:
        """以 epoch + 1 接管：写下 ``writer_takeover`` 记录并成为新的 writer。

        Raises:
            ValueError: ``writer_id`` / ``operation_id`` 为空。
            JournalSessionNotFoundError: Session 不存在。
            JournalBusyError: writer 还活着（不是同一次接管的重试）。
            JournalSessionEndedError: 已有 ``session_ended``。
            JournalConflictError: 同 operation id 的接管已被消费（其后又有新记录）。
        """
        if not writer_id or not operation_id:
            raise ValueError("writer_id and operation_id must be non-empty")
        async with self._storage.lock:
            stored = self._storage.sessions.get(session_id)
            if stored is None:
                raise JournalSessionNotFoundError(session_id)
            holder = stored.holder
            if holder is not None:
                if (
                    holder.owner is self
                    and holder.operation_kind == "open"
                    and holder.operation_id == operation_id
                    and holder.lease.writer_id == writer_id
                    and isinstance(holder.result, SessionOpenResult)
                ):
                    return holder.result
                raise JournalBusyError(
                    session_id, holder.lease.writer_id if holder.owner is self else None)
            ended = ended_record_id(stored.envelopes)
            if ended is not None:
                raise JournalSessionEndedError(session_id, ended)
            tail = stored.envelopes[-1]
            record = build_takeover_record(
                session_id=session_id, writer_id=writer_id, operation_id=operation_id,
                previous_epoch=tail.writer_epoch, previous_tail_seq=tail.seq,
                previous_tail_hash=tail.record_hash,
            )
            existing = stored.index.get(record.record_id)
            if existing is not None:
                # 上一次接管已落库而结果丢失：它仍是尾、且属于同一个 writer 才能复用
                previous_epoch = _reusable_takeover_epoch(tail, record, writer_id)
                ack = existing.ack
            else:
                previous_epoch = tail.writer_epoch
                sealed = seal_batch(
                    (record,), previous_seq=tail.seq, previous_hash=tail.record_hash,
                    writer_epoch=tail.writer_epoch + 1,
                )
                stored.envelopes.extend(sealed.envelopes)
                stored.index.update(sealed.committed())
                ack = sealed.ack
            result = SessionOpenResult(
                lease=SessionLease(
                    session_id=session_id, writer_id=writer_id,
                    writer_epoch=ack.writer_epoch, lease_id=uuid4().hex,
                ),
                ack=ack, previous_epoch=previous_epoch,
            )
            stored.holder = _Holder(
                self, "open", operation_id,
                canonical_hash({"operation_id": operation_id, "writer_id": writer_id}), result,
            )
            return result

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """校验 lease → 幂等判定 → CAS → 整批落库。

        Raises:
            ValueError: 空批次或跨 Session。
            JournalLeaseError: lease 与当前 writer 不完全一致，或本实例不持有 writer。
            JournalConflictError: ``expected_seq`` 过时，或与已提交内容冲突。
        """
        snapshots, fingerprints = snapshot_records(records)
        session_id = snapshots[0].session_id
        async with self._storage.lock:
            stored = self._held(session_id)
            assert stored.holder is not None
            if lease != stored.holder.lease:
                raise JournalLeaseError(session_id, "lease fields do not match")
            existing = resolve_idempotent_ack(snapshots, fingerprints, stored.index.get)
            if existing is not None:
                return existing
            if expected_seq != stored.tail_seq:
                raise JournalConflictError(
                    "expected_seq conflict", expected_seq=expected_seq, actual_seq=stored.tail_seq)
            sealed = seal_batch(
                snapshots, previous_seq=stored.tail_seq, previous_hash=stored.tail_hash,
                writer_epoch=lease.writer_epoch, fingerprints=fingerprints,
            )
            stored.envelopes.extend(sealed.envelopes)
            stored.index.update(sealed.committed())
            return sealed.ack

    def _held(self, session_id: str) -> _StoredSession:
        """本实例持有 writer 的 Session；否则 ``JournalLeaseError``。"""
        stored = self._storage.sessions.get(session_id)
        if stored is None or stored.holder is None or stored.holder.owner is not self:
            raise JournalLeaseError(session_id, "no live writer")
        return stored

    async def close_session(self, lease: SessionLease) -> None:
        """释放一个 Session 的 writer，不写任何记录。

        Raises:
            JournalLeaseError: 本实例不持有该 Session 的 writer，或 lease 不符。
        """
        async with self._storage.lock:
            stored = self._held(lease.session_id)
            assert stored.holder is not None
            if lease != stored.holder.lease:
                raise JournalLeaseError(lease.session_id, "lease fields do not match")
            stored.holder = None

    async def close(self) -> None:
        """释放本实例持有的全部 writer，不写任何记录（等同进程退出）。"""
        async with self._storage.lock:
            for stored in self._storage.sessions.values():
                if stored.holder is not None and stored.holder.owner is self:
                    stored.holder = None

    async def load(
        self, session_id: str, *, after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """已提交的 envelope，按 seq 升序；不存在的 Session 什么都不给。"""
        async with self._storage.lock:
            stored = self._storage.sessions.get(session_id)
            snapshot = tuple(stored.envelopes) if stored is not None else ()
        for envelope in snapshot:
            if envelope.seq > after_seq:
                yield envelope

    async def verify(self, session_id: str) -> JournalVerification:
        """整条链的 strict 校验。

        Raises:
            JournalSessionNotFoundError: Session 不存在。
            JournalIntegrityError: 链不完整。
        """
        async with self._storage.lock:
            stored = self._storage.sessions.get(session_id)
            if stored is None:
                raise JournalSessionNotFoundError(session_id)
            snapshot = tuple(stored.envelopes)
        return verify_envelopes(snapshot, session_id=session_id)


def _reusable_takeover_epoch(tail: JournalEnvelope, record: JournalRecord, writer_id: str) -> int:
    """已存在的同 id 接管记录能否复用；能则返回它的 ``previous_epoch``。

    Raises:
        JournalConflictError: 它不再是尾（该 epoch 已被用来写入），或属于别的 writer。
    """
    previous = tail.payload.get("previous_epoch")
    if (
        tail.record_id != record.record_id
        or tail.record_type != WRITER_TAKEOVER_RECORD_TYPE
        or tail.payload.get("writer_id") != writer_id
        or not isinstance(previous, int)
    ):
        raise JournalConflictError("takeover operation already consumed", record_id=record.record_id)
    return previous


__all__ = ["InMemoryJournalStorage", "InMemorySessionJournalCore"]
