"""SessionJournal Phase 2：跨进程写者互斥（OS 锁）与 open_existing 接管测试。

两个 ``JsonlSessionJournalCore`` 实例指向同一 root 即模拟两个进程：``flock`` 以打开的
文件描述为单位互斥，同进程两个实例与两个进程语义一致。``core.close()`` 释放锁但不写
``session_ended``，用来模拟 writer 进程崩溃。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from taifeng.conversation.journal import (
    WRITER_TAKEOVER_RECORD_TYPE,
    ActorRef,
    JournalBusyError,
    JournalConflictError,
    JournalHealth,
    JournalIntegrityError,
    JournalLeaseError,
    JournalLockUnsupportedError,
    JournalRecord,
    JournalRecoveryRequiredError,
    JournalSessionEndedError,
    JournalSessionNotFoundError,
    RootThreadDescriptor,
    SessionDescriptor,
    SessionLease,
    WriterLockBusyError,
)
from taifeng.conversation.journal import writer_lock as writer_lock_module
from taifeng.conversation.journal.framing import encode_batch
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.writer_lock import FcntlWriterLockAdapter

_SESSION = "ses_1"


def _descriptor() -> SessionDescriptor:
    """构造稳定的 Session 初始化描述符。"""
    return SessionDescriptor(
        session_id=_SESSION,
        creation_operation_id="create_1",
        writer_id="worker_a",
        root_thread=RootThreadDescriptor(thread_id="thr_root", entry_skill_id="general"),
        config={"model": "sim"},
    )


def _record(record_id: str, record_type: str = "test_record") -> JournalRecord:
    """构造追加用 canonical record。"""
    return JournalRecord(
        session_id=_SESSION,
        record_id=record_id,
        record_type=record_type,
        actor=ActorRef(kind="system", source="test"),
        payload={"value": record_id},
    )


async def _crashed_session(root: Path, *extra: JournalRecord) -> None:
    """创建 Session、追加若干记录后只释放锁（不写 session_ended），模拟崩溃。"""
    core = JsonlSessionJournalCore(root)
    created = await core.create_session(_descriptor())
    seq = created.ack.last_seq
    for record in extra:
        ack = await core.append(record, lease=created.lease, expected_seq=seq)
        seq = ack.last_seq
    await core.close()


@pytest.mark.anyio
async def test_create_session_second_core_same_session_raises_busy(tmp_path: Path) -> None:
    """另一实例（进程）持有写者锁时，同 Session 的 create 被 OS 锁拒绝。"""
    first = JsonlSessionJournalCore(tmp_path)
    await first.create_session(_descriptor())
    second = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalBusyError) as caught:
        await second.create_session(_descriptor())

    assert caught.value.writer_id is None
    await first.close()


@pytest.mark.anyio
async def test_open_existing_while_other_core_live_raises_busy(tmp_path: Path) -> None:
    """原 writer 仍存活（持锁）时，接管必须 Busy，不得以更高 epoch 抢写。"""
    first = JsonlSessionJournalCore(tmp_path)
    await first.create_session(_descriptor())
    second = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalBusyError):
        await second.open_existing(_SESSION, writer_id="worker_b", operation_id="open_1")

    assert (await second.verify(_SESSION)).committed_tail_seq == 3
    await first.close()


@pytest.mark.anyio
async def test_open_existing_after_crash_increments_epoch_and_verifies(tmp_path: Path) -> None:
    """崩溃后接管：epoch 1→2、写接管记录、新 lease 可继续追加且 strict verify 通过。"""
    await _crashed_session(tmp_path, _record("rec_1"))
    core = JsonlSessionJournalCore(tmp_path)

    opened = await core.open_existing(_SESSION, writer_id="worker_b", operation_id="open_1")
    ack = await core.append(
        _record("rec_2"), lease=opened.lease, expected_seq=opened.ack.last_seq
    )

    assert opened.previous_epoch == 1
    assert opened.lease.writer_epoch == 2
    assert opened.lease.writer_id == "worker_b"
    assert opened.ack.record_ids == ("open_1:writer_takeover",)
    assert ack.writer_epoch == 2 and ack.first_seq == 6
    envelopes = [envelope async for envelope in core.load(_SESSION)]
    assert [e.writer_epoch for e in envelopes] == [1, 1, 1, 1, 2, 2]
    takeover = envelopes[4]
    assert takeover.record_type == WRITER_TAKEOVER_RECORD_TYPE
    assert takeover.payload["previous_epoch"] == 1
    assert takeover.payload["previous_tail_seq"] == 4
    assert takeover.payload["previous_tail_hash"] == envelopes[3].record_hash
    assert takeover.payload["writer_id"] == "worker_b"
    verification = await core.verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    assert verification.committed_tail_seq == 6
    await core.close()


@pytest.mark.anyio
async def test_open_existing_second_takeover_increments_again(tmp_path: Path) -> None:
    """连续两次崩溃接管：epoch 单步递增到 3，verify 通过。"""
    await _crashed_session(tmp_path)
    second = JsonlSessionJournalCore(tmp_path)
    await second.open_existing(_SESSION, writer_id="w2", operation_id="open_1")
    await second.close()
    third = JsonlSessionJournalCore(tmp_path)

    opened = await third.open_existing(_SESSION, writer_id="w3", operation_id="open_2")

    assert (opened.previous_epoch, opened.lease.writer_epoch) == (2, 3)
    assert (await third.verify(_SESSION)).health is JournalHealth.HEALTHY
    await third.close()


@pytest.mark.anyio
async def test_open_existing_ended_session_raises_session_ended(tmp_path: Path) -> None:
    """已提交 session_ended 的 Session 不可重开，且不写任何接管记录。"""
    await _crashed_session(tmp_path, _record("end_1", "session_ended"))
    core = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalSessionEndedError) as caught:
        await core.open_existing(_SESSION, writer_id="worker_b", operation_id="open_1")

    assert caught.value.ended_record_id == "end_1"
    assert (await core.verify(_SESSION)).committed_tail_seq == 4
    # 失败路径必须释放写者锁：其他实例随后仍可观察到同一拒绝而非 Busy
    other = JsonlSessionJournalCore(tmp_path)
    with pytest.raises(JournalSessionEndedError):
        await other.open_existing(_SESSION, writer_id="worker_c", operation_id="open_2")


@pytest.mark.anyio
async def test_open_existing_torn_tail_raises_recovery_required(tmp_path: Path) -> None:
    """物理尾部残缺（未闭合 batch）时接管 fail closed，文件不被修改。"""
    await _crashed_session(tmp_path, _record("rec_1"))
    path = tmp_path / f"{_SESSION}.journal.jsonl"
    path.write_bytes(path.read_bytes() + b'{"__journal_frame__":"BEG')
    before = path.read_bytes()
    core = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalRecoveryRequiredError) as caught:
        await core.open_existing(_SESSION, writer_id="worker_b", operation_id="open_1")

    assert caught.value.cause == "physical_tail"
    assert caught.value.committed_tail_seq == 4
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_open_existing_missing_session_raises_not_found(tmp_path: Path) -> None:
    """不存在的 Session 显式报错，不隐式创建。"""
    core = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalSessionNotFoundError):
        await core.open_existing("ses_missing", writer_id="w", operation_id="open_1")

    assert not (tmp_path / "ses_missing.journal.jsonl").exists()


@pytest.mark.anyio
async def test_open_existing_same_operation_same_instance_is_idempotent(
    tmp_path: Path,
) -> None:
    """同实例同 operation 重复 open 返回同一结果；不同 operation 得到 Busy。"""
    await _crashed_session(tmp_path)
    core = JsonlSessionJournalCore(tmp_path)

    first = await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")
    again = await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")

    assert again == first
    with pytest.raises(JournalBusyError):
        await core.open_existing(_SESSION, writer_id="w", operation_id="open_2")
    assert (await core.verify(_SESSION)).committed_tail_seq == 4
    await core.close()


@pytest.mark.anyio
async def test_open_existing_retry_after_lost_ack_reuses_durable_takeover(
    tmp_path: Path,
) -> None:
    """接管记录已 durable 但调用方丢了结果：同 operation 重试复用同 epoch，不再写一条。"""
    await _crashed_session(tmp_path)
    lost = JsonlSessionJournalCore(tmp_path)
    original = await lost.open_existing(_SESSION, writer_id="w", operation_id="open_1")
    await lost.close()
    core = JsonlSessionJournalCore(tmp_path)

    retried = await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")

    assert retried.ack == original.ack
    assert retried.lease.writer_epoch == 2
    assert retried.previous_epoch == 1
    assert retried.lease.lease_id != original.lease.lease_id
    assert (await core.verify(_SESSION)).committed_tail_seq == 4
    await core.append(_record("rec_x"), lease=retried.lease, expected_seq=4)
    await core.close()


@pytest.mark.anyio
async def test_open_existing_retry_after_consumed_takeover_raises_conflict(
    tmp_path: Path,
) -> None:
    """同 operation 的接管之后已有新写入：该 epoch 已被使用，重试必须冲突。"""
    await _crashed_session(tmp_path)
    used = JsonlSessionJournalCore(tmp_path)
    opened = await used.open_existing(_SESSION, writer_id="w", operation_id="open_1")
    await used.append(_record("rec_1"), lease=opened.lease, expected_seq=4)
    await used.close()
    core = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalConflictError):
        await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")


@pytest.mark.anyio
async def test_close_session_releases_writer_lock_for_other_core(tmp_path: Path) -> None:
    """close_session 释放 OS 锁后，另一实例可以接管。"""
    first = JsonlSessionJournalCore(tmp_path)
    created = await first.create_session(_descriptor())
    await first.close_session(created.lease)
    second = JsonlSessionJournalCore(tmp_path)

    opened = await second.open_existing(_SESSION, writer_id="w2", operation_id="open_1")

    assert opened.lease.writer_epoch == 2
    await second.close()


@pytest.mark.anyio
async def test_stale_lease_from_previous_epoch_is_rejected(tmp_path: Path) -> None:
    """接管后旧 epoch lease 不能在新 writer 上追加。"""
    await _crashed_session(tmp_path)
    core = JsonlSessionJournalCore(tmp_path)
    opened = await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")
    stale = SessionLease(
        session_id=_SESSION, writer_id="w", writer_epoch=1, lease_id=opened.lease.lease_id
    )

    with pytest.raises(JournalLeaseError):
        await core.append(_record("rec_1"), lease=stale, expected_seq=4)
    await core.close()


def _write_raw_batch(
    path: Path,
    record: JournalRecord,
    *,
    expected_seq: int,
    writer_epoch: int,
    previous_hash: str,
) -> None:
    """绕过 core 直接追加一个自洽 batch（hash chain 正确），用于构造 epoch 违规。"""
    encoded = encode_batch(
        (record,),
        batch_id=f"raw_{expected_seq}",
        expected_seq=expected_seq,
        writer_epoch=writer_epoch,
        previous_hash=previous_hash,
        recorded_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    with path.open("ab") as stream:
        stream.write(b"".join(encoded.lines))


@pytest.mark.anyio
async def test_verify_writer_epoch_regression_raises_integrity(tmp_path: Path) -> None:
    """接管到 epoch 2 后又出现 epoch 1 的 batch：strict verify 拒绝 epoch 递减。"""
    await _crashed_session(tmp_path)
    core = JsonlSessionJournalCore(tmp_path)
    opened = await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")
    await core.close()
    _write_raw_batch(
        tmp_path / f"{_SESSION}.journal.jsonl",
        _record("rec_old"),
        expected_seq=4,
        writer_epoch=1,
        previous_hash=opened.ack.tail_hash,
    )

    with pytest.raises(JournalIntegrityError, match="writer_epoch regression"):
        await core.verify(_SESSION)


@pytest.mark.anyio
async def test_verify_epoch_increase_without_takeover_raises_integrity(
    tmp_path: Path,
) -> None:
    """epoch 上升却不是 writer_takeover 开启：视为未授权写者，strict verify 拒绝。"""
    core = JsonlSessionJournalCore(tmp_path)
    created = await core.create_session(_descriptor())
    await core.close()
    _write_raw_batch(
        tmp_path / f"{_SESSION}.journal.jsonl",
        _record("rec_rogue"),
        expected_seq=3,
        writer_epoch=2,
        previous_hash=created.ack.tail_hash,
    )

    with pytest.raises(JournalIntegrityError, match="without writer_takeover"):
        await core.verify(_SESSION)


def test_fcntl_adapter_non_posix_raises_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 POSIX 平台显式抛错，绝不静默不加锁。"""
    monkeypatch.setattr(writer_lock_module, "_posix_flock_available", lambda: False)

    with pytest.raises(JournalLockUnsupportedError):
        FcntlWriterLockAdapter().acquire(tmp_path / "x.journal.lock")


def test_fcntl_adapter_second_acquire_raises_busy_until_release(tmp_path: Path) -> None:
    """同一锁文件第二次 acquire（独立打开的描述）Busy；释放后可再获取。"""
    adapter = FcntlWriterLockAdapter()
    path = tmp_path / "x.journal.lock"
    handle = adapter.acquire(path)

    with pytest.raises(WriterLockBusyError):
        adapter.acquire(path)
    adapter.release(handle)
    adapter.release(adapter.acquire(path))
    with pytest.raises(RuntimeError, match="already released"):
        adapter.release(handle)


class _RecordingLockAdapter:
    """记录 acquire/release 调用的内存写者锁（验证注入边界）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.held: set[Path] = set()

    def acquire(self, path: Path) -> object:
        """模拟非阻塞独占获取。"""
        if path in self.held:
            raise WriterLockBusyError(path)
        self.held.add(path)
        self.events.append(("acquire", path.name))
        return path

    def release(self, handle: object) -> None:
        """释放内存锁。"""
        assert isinstance(handle, Path)
        self.held.remove(handle)
        self.events.append(("release", handle.name))


@pytest.mark.anyio
async def test_writer_lock_adapter_is_injectable_and_released_on_failure(
    tmp_path: Path,
) -> None:
    """锁经注入边界获取/释放；open_existing 失败路径同样释放锁。"""
    await _crashed_session(tmp_path, _record("end_1", "session_ended"))
    adapter = _RecordingLockAdapter()
    core = JsonlSessionJournalCore(tmp_path, writer_lock_adapter=adapter)

    with pytest.raises(JournalSessionEndedError):
        await core.open_existing(_SESSION, writer_id="w", operation_id="open_1")

    assert adapter.events == [
        ("acquire", f"{_SESSION}.journal.lock"),
        ("release", f"{_SESSION}.journal.lock"),
    ]
    assert adapter.held == set()
