"""SessionJournal Phase 1 的异步 durable JSONL core。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import anyio

from taifeng.conversation.journal.canonical import (
    canonical_hash,
    model_canonical_data,
    record_fingerprint,
)
from taifeng.conversation.journal.errors import (
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalRecoveryRequiredError,
)
from taifeng.conversation.journal.framing import (
    DecodedJournal,
    decode_committed_lines,
    encode_batch,
)
from taifeng.conversation.journal.models import (
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    build_initialization_records,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ZERO_HASH = "0" * 64


class _CommitOutcomeUncertainError(Exception):
    """文件可能已被部分或完整修改，调用方不能安全重试。"""


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


class SyncFileAdapter(Protocol):
    """注入同步文件边界，core 统一在线程池调用。"""

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """独占创建、写入并 fsync 文件及父目录。"""

    def read_bytes(self, path: Path) -> bytes:
        """读取完整物理 Journal bytes。"""

    def append_durable(self, path: Path, payload: bytes) -> None:
        """追加完整 batch，并在返回前 flush+fsync。"""


class DefaultSyncFileAdapter:
    """基于本地文件系统的 durable 同步适配器。"""

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """以 ``xb`` 独占创建，并在返回前完成 file/directory fsync。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def read_bytes(self, path: Path) -> bytes:
        """读取物理文件；不存在时由调用方决定语义。"""
        return path.read_bytes()

    def append_durable(self, path: Path, payload: bytes) -> None:
        """追加 batch，并在返回前完成 file flush+fsync。"""
        stream = path.open("ab")
        try:
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise _CommitOutcomeUncertainError from exc


@dataclass(frozen=True)
class _CommittedRecord:
    """幂等索引中的 caller fingerprint 与原 batch ack。"""

    fingerprint: str
    ack: JournalAck


@dataclass
class _LiveWriter:
    """一个 core 实例内的 live writer 状态。"""

    descriptor_fingerprint: str
    creation_operation_id: str
    result: SessionCreateResult
    lock: anyio.Lock
    committed_tail_seq: int
    committed_tail_hash: str
    committed_by_record_id: dict[str, _CommittedRecord]
    recovery_required: bool = False
    closed: bool = False


class JsonlSessionJournalCore:
    """隔离的 Phase 1 SessionJournal JSONL 实现。"""

    def __init__(
        self,
        root: str | Path,
        *,
        sync_file_adapter: SyncFileAdapter | None = None,
        commit_timeout: float = 30.0,
    ) -> None:
        """配置 root 与可注入同步 IO 边界，不在构造时触碰文件系统。"""
        if commit_timeout <= 0:
            raise ValueError("commit_timeout must be positive")
        self._root = Path(root).expanduser().resolve()
        self._sync_file_adapter = sync_file_adapter or DefaultSyncFileAdapter()
        self._commit_timeout = commit_timeout
        self._registry_lock = anyio.Lock()
        self._writers: dict[str, _LiveWriter] = {}

    def _session_path(self, session_id: str) -> Path:
        """把安全 session id 映射为 root 下的单一文件。"""
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError(f"unsafe session_id: {session_id!r}")
        return self._root / f"{session_id}.journal.jsonl"

    async def create_session(self, descriptor: SessionDescriptor) -> SessionCreateResult:
        """独占创建 Session，并 durable commit 三记录初始化 batch。"""
        descriptor = SessionDescriptor.model_validate(
            descriptor.model_dump(mode="python")
        )
        records = build_initialization_records(descriptor)
        path = self._session_path(descriptor.session_id)
        fingerprint = canonical_hash(model_canonical_data(descriptor))
        async with self._registry_lock:
            live = self._writers.get(descriptor.session_id)
            if live is not None:
                if (
                    live.creation_operation_id == descriptor.creation_operation_id
                    and live.result.lease.writer_id == descriptor.writer_id
                    and live.descriptor_fingerprint == fingerprint
                ):
                    return live.result
                raise JournalBusyError(
                    descriptor.session_id,
                    live.result.lease.writer_id,
                )
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
                await self._run_sync_commit(
                    self._sync_file_adapter.create_exclusive,
                    path,
                    b"".join(encoded.lines),
                )
            except FileExistsError as exc:
                raise JournalAlreadyExistsError(descriptor.session_id) from exc
            except TimeoutError as exc:
                raise JournalRecoveryRequiredError(descriptor.session_id, 0) from exc
            result = SessionCreateResult(lease=lease, ack=encoded.ack)
            self._writers[descriptor.session_id] = _LiveWriter(
                descriptor_fingerprint=fingerprint,
                creation_operation_id=descriptor.creation_operation_id,
                result=result,
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
            return result

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

    async def close(self) -> None:
        """等待在途写完成并清理 live leases，不追加领域 record。"""
        async with self._registry_lock:
            writers = tuple(self._writers.values())
            for writer in writers:
                await writer.lock.acquire()
            try:
                for writer in writers:
                    writer.closed = True
                self._writers.clear()
            finally:
                for writer in reversed(writers):
                    writer.lock.release()

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
        records = tuple(
            JournalRecord.model_validate(record.model_dump(mode="python"))
            for record in records
        )
        session_id = records[0].session_id
        if any(record.session_id != session_id for record in records):
            raise ValueError("all records in a batch must belong to one session")
        writer = self._writers.get(session_id)
        if writer is None:
            raise JournalLeaseError(session_id, "no live writer")
        async with writer.lock:
            if writer.closed:
                raise JournalLeaseError(session_id, "writer closed")
            if writer.recovery_required:
                raise JournalRecoveryRequiredError(
                    session_id,
                    writer.committed_tail_seq,
                )
            scanned = await self._scan(session_id)
            if scanned.verification.health is JournalHealth.RECOVERY_REQUIRED:
                writer.recovery_required = True
                raise JournalRecoveryRequiredError(
                    session_id,
                    scanned.verification.committed_tail_seq,
                )
            if (
                scanned.verification.committed_tail_seq != writer.committed_tail_seq
                or scanned.verification.committed_tail_hash != writer.committed_tail_hash
            ):
                raise JournalIntegrityError("live writer tail mismatch")
            self._validate_lease(lease, writer)
            existing_ack = self._idempotent_ack(records, writer)
            if existing_ack is not None:
                return existing_ack
            if expected_seq != writer.committed_tail_seq:
                raise JournalConflictError(
                    "expected_seq conflict",
                    expected_seq=expected_seq,
                    actual_seq=writer.committed_tail_seq,
                )
            return await self._commit_new_batch(records, writer)

    def _idempotent_ack(
        self,
        records: tuple[JournalRecord, ...],
        writer: _LiveWriter,
    ) -> JournalAck | None:
        """在 CAS 前检查完整原 batch 重试；任何部分命中均 fail closed。"""
        indexed = tuple(
            writer.committed_by_record_id.get(record.record_id) for record in records
        )
        if not any(item is not None for item in indexed):
            return None
        if not all(item is not None for item in indexed):
            raise JournalConflictError("batch idempotency conflict")
        committed = tuple(item for item in indexed if item is not None)
        fingerprints = tuple(record_fingerprint(record) for record in records)
        if any(
            item.fingerprint != fingerprint
            for item, fingerprint in zip(committed, fingerprints, strict=True)
        ):
            if len(records) == 1:
                raise JournalConflictError(
                    "record content conflict",
                    record_id=records[0].record_id,
                )
            raise JournalConflictError("batch idempotency conflict")
        ack = committed[0].ack
        if any(item.ack != ack for item in committed) or ack.record_ids != tuple(
            record.record_id for record in records
        ):
            raise JournalConflictError("batch idempotency conflict")
        return ack

    def _validate_lease(self, lease: SessionLease, writer: _LiveWriter) -> None:
        """要求 lease capability 与当前 live writer 全字段一致。"""
        if lease != writer.result.lease:
            raise JournalLeaseError(lease.session_id, "lease fields do not match")

    async def _commit_new_batch(
        self,
        records: tuple[JournalRecord, ...],
        writer: _LiveWriter,
    ) -> JournalAck:
        """生成、持久化 batch，并只在 fsync 成功后推进内存 tail/index。"""
        lease = writer.result.lease
        fingerprints = tuple(record_fingerprint(record) for record in records)
        encoded = encode_batch(
            records,
            batch_id=uuid4().hex,
            expected_seq=writer.committed_tail_seq,
            writer_epoch=lease.writer_epoch,
            previous_hash=writer.committed_tail_hash,
            recorded_at=datetime.now(UTC),
        )
        path = self._session_path(lease.session_id)
        try:
            await self._run_sync_commit(
                self._sync_file_adapter.append_durable,
                path,
                b"".join(encoded.lines),
            )
        except (TimeoutError, _CommitOutcomeUncertainError) as exc:
            writer.recovery_required = True
            raise JournalRecoveryRequiredError(
                lease.session_id,
                writer.committed_tail_seq,
            ) from exc
        for record, fingerprint in zip(records, fingerprints, strict=True):
            writer.committed_by_record_id[record.record_id] = _CommittedRecord(
                fingerprint=fingerprint,
                ack=encoded.ack,
            )
        writer.committed_tail_seq = encoded.ack.last_seq
        writer.committed_tail_hash = encoded.ack.tail_hash
        return encoded.ack

    async def _run_sync_commit(
        self,
        function: Callable[..., None],
        *args: object,
    ) -> None:
        """提交前接受取消；提交开始后 shield，并以 deadline 收敛未知结果。"""
        await anyio.lowlevel.checkpoint_if_cancelled()
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
