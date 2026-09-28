"""SessionJournal 的异步 durable JSONL core。

Phase 1：原子 batch、hash chain、同进程 live writer fencing、strict load/verify。
Phase 2：OS 级跨进程写者锁（``writer_lock``）与 ``open_existing`` 以更高 epoch 接管。
"""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio

from taifeng.conversation.journal.canonical import (
    canonical_hash,
    model_canonical_data,
    record_fingerprint,
)
from taifeng.conversation.journal.errors import (
    CommitNotStartedError,
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalRecoveryRequiredError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
)
from taifeng.conversation.journal.file_io import (
    DefaultSyncFileAdapter,
    SyncFileAdapter,
)
from taifeng.conversation.journal.framing import (
    DecodedJournal,
    decode_committed_lines,
    encode_batch,
)
from taifeng.conversation.journal.models import (
    SESSION_ENDED_RECORD_TYPE,
    WRITER_TAKEOVER_RECORD_TYPE,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    SessionOpenResult,
    build_initialization_records,
    build_takeover_record,
)
from taifeng.conversation.journal.writer_lock import (
    FcntlWriterLockAdapter,
    WriterLockAdapter,
    WriterLockBusyError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ZERO_HASH = "0" * 64


def _validate_committed_record_ids(decoded: DecodedJournal) -> None:
    """拒绝 committed 区域中相同 id 的不同 caller fingerprint。"""
    fingerprints: dict[str, str] = {}
    for batch in decoded.batches:
        for envelope, fingerprint in zip(
            batch.envelopes,
            batch.fingerprints,
            strict=True,
        ):
            existing = fingerprints.get(envelope.record_id)
            if existing is not None and existing != fingerprint:
                raise JournalIntegrityError(
                    f"conflicting duplicate record_id: {envelope.record_id}"
                )
            fingerprints[envelope.record_id] = fingerprint


def _decode_physical(payload: bytes, *, session_id: str) -> DecodedJournal:
    """strict decode 物理 bytes，并把无换行最终行收敛为 torn tail。"""
    lines = payload.splitlines(keepends=True)
    physical_tail_torn = bool(payload) and not payload.endswith(b"\n")
    complete_lines = lines[:-1] if physical_tail_torn else lines
    decoded = decode_committed_lines(complete_lines, session_id=session_id)
    _validate_committed_record_ids(decoded)
    if not physical_tail_torn:
        return decoded
    verification = decoded.verification.model_copy(
        update={
            "health": JournalHealth.RECOVERY_REQUIRED,
            "physical_tail_torn": True,
        }
    )
    return DecodedJournal(decoded.envelopes, decoded.batches, verification)


@dataclass(frozen=True)
class _CommittedRecord:
    """幂等索引中的 caller fingerprint 与原 batch ack。"""

    fingerprint: str
    ack: JournalAck


@dataclass(frozen=True)
class _AppendSnapshot:
    """append 在任何 await 前固定的 records 与 caller fingerprints。"""

    records: tuple[JournalRecord, ...]
    fingerprints: tuple[str, ...]


@dataclass
class _LiveWriter:
    """一个 core 实例内的 live writer 状态。

    ``operation_kind`` 区分 create / open 两种取得 writer 的方式，二者的幂等重试
    只在同 kind、同 operation id、同 writer、同 fingerprint 时命中。``lock_handle``
    是跨进程写者锁 handle，随 writer 一起在 close_session / close 时释放。
    """

    operation_kind: str
    operation_id: str
    operation_fingerprint: str
    result: SessionCreateResult | SessionOpenResult
    lock: anyio.Lock
    committed_tail_seq: int
    committed_tail_hash: str
    committed_by_record_id: dict[str, _CommittedRecord]
    lock_handle: object
    recovery_required: bool = False
    recovery_cause: str | None = None
    closed: bool = False


def _committed_index(decoded: DecodedJournal) -> dict[str, _CommittedRecord]:
    """从 strict scan 结果重建 record id → (fingerprint, 原 batch ack) 幂等索引。"""
    return {
        envelope.record_id: _CommittedRecord(fingerprint=fingerprint, ack=batch.ack)
        for batch in decoded.batches
        for envelope, fingerprint in zip(batch.envelopes, batch.fingerprints, strict=True)
    }


def _ended_record_id(decoded: DecodedJournal) -> str | None:
    """返回已提交 ``session_ended`` 的 record id；没有则 None。"""
    for envelope in decoded.envelopes:
        if envelope.record_type == SESSION_ENDED_RECORD_TYPE:
            return envelope.record_id
    return None


def _snapshot_records(records: tuple[JournalRecord, ...]) -> _AppendSnapshot:
    """同步重建 caller DTO，并只从该快照预计算一次 fingerprint。"""
    snapshots = tuple(
        JournalRecord.model_validate(record.model_dump(mode="python"))
        for record in records
    )
    fingerprints = tuple(record_fingerprint(record) for record in snapshots)
    return _AppendSnapshot(snapshots, fingerprints)


class JsonlSessionJournalCore:
    """SessionJournal JSONL 实现：同进程 lease fencing + 跨进程 OS 写者锁。"""

    def __init__(
        self,
        root: str | Path,
        *,
        sync_file_adapter: SyncFileAdapter | None = None,
        writer_lock_adapter: WriterLockAdapter | None = None,
        commit_timeout: float = 30.0,
    ) -> None:
        """配置 root 与可注入同步 IO / 写者锁边界，不在构造时触碰文件系统。"""
        if commit_timeout <= 0:
            raise ValueError("commit_timeout must be positive")
        self._root = Path(root).expanduser().resolve()
        self._sync_file_adapter = sync_file_adapter or DefaultSyncFileAdapter()
        self._writer_lock_adapter = writer_lock_adapter or FcntlWriterLockAdapter()
        self._commit_timeout = commit_timeout
        self._registry_lock = anyio.Lock()
        self._writers: dict[str, _LiveWriter] = {}
        # 尚未登记 writer 就结果未知的 create/open：session → 冻结时 committed tail
        self._creation_recovery_required: dict[str, int] = {}
        # 结果未知时不释放的写者锁（后台线程可能仍在写），只由 close() 释放
        self._parked_locks: dict[str, object] = {}

    def _session_path(self, session_id: str) -> Path:
        """把安全 session id 映射为 root 下的单一文件。"""
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self._root / f"{session_id}.journal.jsonl"

    def _lock_path(self, session_id: str) -> Path:
        """Session 的跨进程写者锁文件（与 Journal 同目录，永不删除）。"""
        return self._session_path(session_id).with_name(f"{session_id}.journal.lock")

    async def _acquire_writer_lock(self, session_id: str) -> object:
        """在线程池非阻塞获取 OS 写者锁；被其他进程 / 实例持有时抛 Busy。"""
        path = self._lock_path(session_id)
        try:
            return await anyio.to_thread.run_sync(
                self._writer_lock_adapter.acquire, path
            )
        except WriterLockBusyError:
            raise JournalBusyError(session_id, None) from None

    async def _release_writer_lock(self, handle: object) -> None:
        """shield 内释放写者锁，调用方取消不能泄漏 fd / 锁。"""
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self._writer_lock_adapter.release, handle)

    async def _release_unless_parked(self, session_id: str, handle: object) -> None:
        """失败路径释放本次获取的锁；已因结果未知而 park 的锁留给 close()。"""
        if self._parked_locks.get(session_id) is not handle:
            await self._release_writer_lock(handle)

    async def create_session(self, descriptor: SessionDescriptor) -> SessionCreateResult:
        """独占创建 Session，并 durable commit 三记录初始化 batch。"""
        descriptor = SessionDescriptor.model_validate(
            descriptor.model_dump(mode="python")
        )
        records = build_initialization_records(descriptor)
        path = self._session_path(descriptor.session_id)
        fingerprint = canonical_hash(model_canonical_data(descriptor))
        async with self._registry_lock:
            if descriptor.session_id in self._creation_recovery_required:
                raise self._creation_recovery_error(descriptor.session_id)
            live = self._writers.get(descriptor.session_id)
            if live is not None:
                if (
                    live.operation_kind == "create"
                    and live.operation_id == descriptor.creation_operation_id
                    and live.result.lease.writer_id == descriptor.writer_id
                    and live.operation_fingerprint == fingerprint
                    and isinstance(live.result, SessionCreateResult)
                ):
                    return live.result
                raise JournalBusyError(
                    descriptor.session_id,
                    live.result.lease.writer_id,
                )
            handle = await self._acquire_writer_lock(descriptor.session_id)
            try:
                result, writer = await self._create_locked(
                    descriptor, records, path, fingerprint, handle
                )
            except BaseException:
                await self._release_unless_parked(descriptor.session_id, handle)
                raise
            self._writers[descriptor.session_id] = writer
            return result

    async def _create_locked(
        self,
        descriptor: SessionDescriptor,
        records: tuple[JournalRecord, JournalRecord, JournalRecord],
        path: Path,
        fingerprint: str,
        handle: object,
    ) -> tuple[SessionCreateResult, _LiveWriter]:
        """持有写者锁后独占创建文件并提交初始化 batch。"""
        lease = SessionLease(
            session_id=descriptor.session_id,
            writer_id=descriptor.writer_id,
            writer_epoch=1,
            lease_id=uuid4().hex,
        )
        encoded = encode_batch(
            records,
            batch_id=f"{descriptor.creation_operation_id}:init",
            expected_seq=0,
            writer_epoch=lease.writer_epoch,
            previous_hash=_ZERO_HASH,
            recorded_at=datetime.now(UTC),
        )
        try:
            await self._execute_commit(
                self._sync_file_adapter.create_exclusive,
                path,
                b"".join(encoded.lines),
                on_outcome_unknown=lambda: self._freeze_creation(
                    descriptor.session_id, handle, committed_tail_seq=0
                ),
            )
        except FileExistsError as exc:
            raise JournalAlreadyExistsError(descriptor.session_id) from exc
        result = SessionCreateResult(lease=lease, ack=encoded.ack)
        writer = _LiveWriter(
            operation_kind="create",
            operation_id=descriptor.creation_operation_id,
            operation_fingerprint=fingerprint,
            result=result,
            lock_handle=handle,
            lock=anyio.Lock(),
            committed_tail_seq=encoded.ack.last_seq,
            committed_tail_hash=encoded.ack.tail_hash,
            committed_by_record_id={
                record.record_id: _CommittedRecord(
                    fingerprint=record_fingerprint(record),
                    ack=encoded.ack,
                )
                for record in records
            },
        )
        return result, writer

    async def open_existing(
        self,
        session_id: str,
        *,
        writer_id: str,
        operation_id: str,
    ) -> SessionOpenResult:
        """跨进程接管已有 Session：持 OS 写者锁、strict verify 后以 epoch+1 写接管记录。

        Raises:
            JournalBusyError: 本实例或其他进程 / 实例仍持有该 Session 的 writer。
            JournalSessionNotFoundError: Journal 文件不存在。
            JournalRecoveryRequiredError: 物理尾部损坏 / 未闭合 batch，或接管提交结果未知。
            JournalSessionEndedError: 已有 durable ``session_ended``，终结的 Session 不可重开。
            JournalConflictError: 同 operation id 的接管已被消费（其后又有新记录）。
            JournalIntegrityError: committed 区域 strict 校验失败。
        """
        if not writer_id or not operation_id:
            raise ValueError("writer_id and operation_id must be non-empty")
        path = self._session_path(session_id)
        async with self._registry_lock:
            if session_id in self._creation_recovery_required:
                raise self._creation_recovery_error(session_id)
            live = self._writers.get(session_id)
            if live is not None:
                # 同实例同 operation 的重复 open 幂等返回原结果；其余一律 Busy
                if (
                    live.operation_kind == "open"
                    and live.operation_id == operation_id
                    and live.result.lease.writer_id == writer_id
                    and isinstance(live.result, SessionOpenResult)
                ):
                    return live.result
                raise JournalBusyError(session_id, live.result.lease.writer_id)
            handle = await self._acquire_writer_lock(session_id)
            try:
                result, writer = await self._open_locked(
                    session_id, path, writer_id, operation_id, handle
                )
            except BaseException:
                await self._release_unless_parked(session_id, handle)
                raise
            self._writers[session_id] = writer
            return result

    async def _scan_for_takeover(self, session_id: str) -> DecodedJournal:
        """strict scan 并拒绝缺失、尾损、空文件与已终结 Session。"""
        try:
            scanned = await self._scan(session_id)
        except FileNotFoundError:
            raise JournalSessionNotFoundError(session_id) from None
        verification = scanned.verification
        if verification.health is JournalHealth.RECOVERY_REQUIRED:
            raise JournalRecoveryRequiredError(
                session_id,
                verification.committed_tail_seq,
                cause="physical_tail",
            )
        if not scanned.envelopes:
            raise JournalIntegrityError("session has no committed records")
        ended = _ended_record_id(scanned)
        if ended is not None:
            raise JournalSessionEndedError(session_id, ended)
        return scanned

    async def _open_locked(
        self,
        session_id: str,
        path: Path,
        writer_id: str,
        operation_id: str,
        handle: object,
    ) -> tuple[SessionOpenResult, _LiveWriter]:
        """持有写者锁后提交（或幂等识别）接管记录，并构造新 epoch live writer。"""
        scanned = await self._scan_for_takeover(session_id)
        index = _committed_index(scanned)
        tail = scanned.envelopes[-1]
        record = build_takeover_record(
            session_id=session_id,
            writer_id=writer_id,
            operation_id=operation_id,
            previous_epoch=tail.writer_epoch,
            previous_tail_seq=tail.seq,
            previous_tail_hash=tail.record_hash,
        )
        existing = index.get(record.record_id)
        if existing is not None:
            # 同 operation 的接管已 durable（上次 ack 丢失）：只有它仍是 tail 且属于
            # 同一 writer 才可复用其 epoch，否则该 epoch 可能已被别人用来写入。
            previous_epoch = self._reusable_takeover_epoch(tail, record, writer_id)
            ack = existing.ack
        else:
            previous_epoch = tail.writer_epoch
            ack = await self._commit_takeover(record, tail, path, handle)
            index[record.record_id] = _CommittedRecord(record_fingerprint(record), ack)
        lease = SessionLease(
            session_id=session_id,
            writer_id=writer_id,
            writer_epoch=ack.writer_epoch,
            lease_id=uuid4().hex,
        )
        result = SessionOpenResult(lease=lease, ack=ack, previous_epoch=previous_epoch)
        writer = _LiveWriter(
            operation_kind="open",
            operation_id=operation_id,
            operation_fingerprint=canonical_hash(
                {"operation_id": operation_id, "writer_id": writer_id}
            ),
            result=result,
            lock_handle=handle,
            lock=anyio.Lock(),
            committed_tail_seq=ack.last_seq,
            committed_tail_hash=ack.tail_hash,
            committed_by_record_id=index,
        )
        return result, writer

    @staticmethod
    def _reusable_takeover_epoch(
        tail: JournalEnvelope,
        record: JournalRecord,
        writer_id: str,
    ) -> int:
        """校验已存在的同 id 接管记录可幂等复用，返回其 previous_epoch。"""
        payload = tail.payload
        previous = payload.get("previous_epoch")
        if (
            tail.record_id != record.record_id
            or tail.record_type != WRITER_TAKEOVER_RECORD_TYPE
            or payload.get("writer_id") != writer_id
            or not isinstance(previous, int)
        ):
            raise JournalConflictError(
                "takeover operation already consumed",
                record_id=record.record_id,
            )
        return previous

    async def _commit_takeover(
        self,
        record: JournalRecord,
        tail: JournalEnvelope,
        path: Path,
        handle: object,
    ) -> JournalAck:
        """以 epoch+1 durable 追加单条接管记录；结果未知冻结 Session 并 park 锁。"""
        encoded = encode_batch(
            (record,),
            batch_id=f"{record.operation_id}:takeover",
            expected_seq=tail.seq,
            writer_epoch=tail.writer_epoch + 1,
            previous_hash=tail.record_hash,
            recorded_at=datetime.now(UTC),
        )
        await self._execute_commit(
            self._sync_file_adapter.append_durable,
            path,
            b"".join(encoded.lines),
            on_outcome_unknown=lambda: self._freeze_creation(
                record.session_id, handle, committed_tail_seq=tail.seq
            ),
        )
        return encoded.ack

    async def append(
        self,
        record: JournalRecord,
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """把单条 record 作为一个 durable batch 追加。"""
        return await self.append_batch(
            (record,),
            lease=lease,
            expected_seq=expected_seq,
        )

    async def close_session(self, lease: SessionLease) -> None:
        """验证 lease 后只释放一个 Session 的 live writer 及其跨进程写者锁。"""
        async with self._registry_lock:
            writer = self._writers.get(lease.session_id)
            if writer is None:
                raise JournalLeaseError(lease.session_id, "no live writer")
            async with writer.lock:
                self._validate_lease(lease, writer)
                writer.closed = True
                self._writers.pop(lease.session_id, None)
                await self._release_writer_lock(writer.lock_handle)

    async def close(self) -> None:
        """等待在途写完成、清理 live leases 并释放全部写者锁，不追加领域 record。"""
        async with self._registry_lock:
            writers = tuple(self._writers.values())
            async with AsyncExitStack() as stack:
                for writer in writers:
                    await stack.enter_async_context(writer.lock)
                for writer in writers:
                    writer.closed = True
                self._writers.clear()
                parked = tuple(self._parked_locks.values())
                self._parked_locks.clear()
                for handle in (*(w.lock_handle for w in writers), *parked):
                    await self._release_writer_lock(handle)

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """在 per-session lock 内按幂等、lease、CAS 顺序 durable 追加。"""
        if not records:
            raise ValueError("journal batch must contain at least one record")
        snapshot = _snapshot_records(records)
        session_id = snapshot.records[0].session_id
        if any(record.session_id != session_id for record in snapshot.records):
            raise ValueError("all records in a batch must belong to one session")
        writer = self._writers.get(session_id)
        if writer is None:
            raise JournalLeaseError(session_id, "no live writer")
        async with writer.lock:
            self._validate_lease(lease, writer)
            if writer.closed:
                raise JournalLeaseError(session_id, "writer closed")
            if writer.recovery_required:
                raise self._writer_recovery_error(writer)
            scanned = await self._scan(session_id)
            if scanned.verification.health is JournalHealth.RECOVERY_REQUIRED:
                writer.recovery_required = True
                writer.recovery_cause = "physical_tail"
                raise self._writer_recovery_error(
                    writer,
                    committed_tail_seq=scanned.verification.committed_tail_seq,
                )
            if (
                scanned.verification.committed_tail_seq != writer.committed_tail_seq
                or scanned.verification.committed_tail_hash != writer.committed_tail_hash
            ):
                raise JournalIntegrityError("live writer tail mismatch")
            existing_ack = self._idempotent_ack(snapshot, writer)
            if existing_ack is not None:
                return existing_ack
            if expected_seq != writer.committed_tail_seq:
                raise JournalConflictError(
                    "expected_seq conflict",
                    expected_seq=expected_seq,
                    actual_seq=writer.committed_tail_seq,
                )
            return await self._commit_new_batch(snapshot, writer)

    def _idempotent_ack(
        self,
        snapshot: _AppendSnapshot,
        writer: _LiveWriter,
    ) -> JournalAck | None:
        """在 CAS 前检查完整原 batch 重试；任何部分命中均 fail closed。"""
        indexed = tuple(
            writer.committed_by_record_id.get(record.record_id)
            for record in snapshot.records
        )
        if not any(item is not None for item in indexed):
            return None
        if not all(item is not None for item in indexed):
            raise JournalConflictError("batch idempotency conflict")
        committed = tuple(item for item in indexed if item is not None)
        if any(
            item.fingerprint != fingerprint
            for item, fingerprint in zip(
                committed,
                snapshot.fingerprints,
                strict=True,
            )
        ):
            if len(snapshot.records) == 1:
                raise JournalConflictError(
                    "record content conflict",
                    record_id=snapshot.records[0].record_id,
                )
            raise JournalConflictError("batch idempotency conflict")
        ack = committed[0].ack
        if any(item.ack != ack for item in committed) or ack.record_ids != tuple(
            record.record_id for record in snapshot.records
        ):
            raise JournalConflictError("batch idempotency conflict")
        return ack

    def _validate_lease(self, lease: SessionLease, writer: _LiveWriter) -> None:
        """要求 lease capability 与当前 live writer 全字段一致。"""
        if lease != writer.result.lease:
            raise JournalLeaseError(
                writer.result.lease.session_id,
                "lease fields do not match",
            )

    def _writer_recovery_error(
        self,
        writer: _LiveWriter,
        *,
        committed_tail_seq: int | None = None,
    ) -> JournalRecoveryRequiredError:
        """从冻结 writer 构造不含底层异常文本的稳定恢复错误。"""
        return JournalRecoveryRequiredError(
            writer.result.lease.session_id,
            writer.committed_tail_seq
            if committed_tail_seq is None
            else committed_tail_seq,
            cause=writer.recovery_cause or "physical_state_unknown",
        )

    def _creation_recovery_error(
        self,
        session_id: str,
    ) -> JournalRecoveryRequiredError:
        """构造 create/open 提交结果未知的稳定、脱敏恢复错误。"""
        return JournalRecoveryRequiredError(
            session_id,
            self._creation_recovery_required[session_id],
            cause="commit_outcome_unknown",
        )

    def _freeze_creation(
        self,
        session_id: str,
        handle: object,
        *,
        committed_tail_seq: int,
    ) -> JournalRecoveryRequiredError:
        """冻结尚未注册 writer 的 create/open，park 写者锁，并返回稳定错误。

        结果未知时后台线程可能仍在写文件，此时释放锁会让另一进程读到半截 batch
        后接管；因此锁保留到 ``close()``。
        """
        self._creation_recovery_required[session_id] = committed_tail_seq
        self._parked_locks[session_id] = handle
        return self._creation_recovery_error(session_id)

    def _freeze_writer(self, writer: _LiveWriter) -> JournalRecoveryRequiredError:
        """冻结已 dispatch 的 append writer，并返回稳定错误。"""
        writer.recovery_required = True
        writer.recovery_cause = "commit_outcome_unknown"
        return self._writer_recovery_error(writer)

    async def _commit_new_batch(
        self,
        snapshot: _AppendSnapshot,
        writer: _LiveWriter,
    ) -> JournalAck:
        """生成、持久化 batch，并只在 fsync 成功后推进内存 tail/index。"""
        lease = writer.result.lease
        encoded = encode_batch(
            snapshot.records,
            batch_id=uuid4().hex,
            expected_seq=writer.committed_tail_seq,
            writer_epoch=lease.writer_epoch,
            previous_hash=writer.committed_tail_hash,
            recorded_at=datetime.now(UTC),
            record_fingerprints=snapshot.fingerprints,
        )
        path = self._session_path(lease.session_id)
        await self._execute_commit(
            self._sync_file_adapter.append_durable,
            path,
            b"".join(encoded.lines),
            on_outcome_unknown=lambda: self._freeze_writer(writer),
        )
        for record, fingerprint in zip(
            snapshot.records,
            snapshot.fingerprints,
            strict=True,
        ):
            writer.committed_by_record_id[record.record_id] = _CommittedRecord(
                fingerprint=fingerprint,
                ack=encoded.ack,
            )
        writer.committed_tail_seq = encoded.ack.last_seq
        writer.committed_tail_hash = encoded.ack.tail_hash
        return encoded.ack

    async def _execute_commit(
        self,
        function: Callable[..., None],
        *args: object,
        on_outcome_unknown: Callable[[], JournalRecoveryRequiredError],
    ) -> None:
        """统一分类 prewrite、fatal 与普通 post-dispatch commit 结果。"""
        prewrite_error: BaseException | None = None
        recovery_error: JournalRecoveryRequiredError | None = None
        fatal_error: KeyboardInterrupt | SystemExit | None = None
        try:
            await self._run_sync_commit(function, *args)
        except CommitNotStartedError as exc:
            prewrite_error = exc.error
        except (KeyboardInterrupt, SystemExit) as exc:
            recovery_error = on_outcome_unknown()
            fatal_error = exc
        except BaseException:
            recovery_error = on_outcome_unknown()
        if prewrite_error is not None:
            raise prewrite_error from None
        if fatal_error is not None:
            raise fatal_error from None
        if recovery_error is not None:
            raise recovery_error from None

    async def _run_sync_commit(
        self,
        function: Callable[..., None],
        *args: object,
    ) -> None:
        """提交前接受取消；提交开始后 shield，并以 deadline 收敛未知结果。"""
        try:
            await anyio.lowlevel.checkpoint_if_cancelled()
        except BaseException as exc:
            raise CommitNotStartedError(exc) from None
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(self._commit_timeout):
                await anyio.to_thread.run_sync(
                    function,
                    *args,
                    abandon_on_cancel=True,
                )

    async def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """strict 读取已 committed envelopes，partial/torn batch 保持不可见。"""
        try:
            decoded = await self._scan(session_id)
        except FileNotFoundError:
            return
        for envelope in decoded.envelopes:
            if envelope.seq > after_seq:
                yield envelope

    async def verify(self, session_id: str) -> JournalVerification:
        """在线程池 strict scan，并返回 committed tail 与物理尾健康状态。"""
        return (await self._scan(session_id)).verification

    async def _scan(self, session_id: str) -> DecodedJournal:
        """把同步读取和完整 codec 校验整体派发到 worker thread。"""
        path = self._session_path(session_id)
        return await anyio.to_thread.run_sync(self._scan_sync, path, session_id)

    def _scan_sync(self, path: Path, session_id: str) -> DecodedJournal:
        """同步读取并 strict decode 一个 Session 文件。"""
        payload = self._sync_file_adapter.read_bytes(path)
        return _decode_physical(payload, session_id=session_id)


__all__ = [
    "DefaultSyncFileAdapter",
    "JsonlSessionJournalCore",
    "SyncFileAdapter",
]
