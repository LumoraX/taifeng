"""SessionJournal JSONL durable core 的创建与追加行为测试。"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    JournalAck,
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalLeaseError,
    JournalRecord,
    JournalRecoveryRequiredError,
    RootThreadDescriptor,
    SessionDescriptor,
    SessionLease,
)
from taifeng.conversation.journal.jsonl import (
    DefaultSyncFileAdapter,
    JsonlSessionJournalCore,
)

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JsonValue


def _descriptor(
    *,
    session_id: str = "ses_1",
    creation_operation_id: str = "create_1",
    writer_id: str = "worker_a",
) -> SessionDescriptor:
    """构造稳定的 Session 初始化描述符。"""
    return SessionDescriptor(
        session_id=session_id,
        creation_operation_id=creation_operation_id,
        writer_id=writer_id,
        root_thread=RootThreadDescriptor(
            thread_id="thr_root",
            entry_skill_id="general",
        ),
        config={"model": "sim"},
    )


def _record(
    *,
    record_id: str = "rec_1",
    payload: dict[str, JsonValue] | None = None,
) -> JournalRecord:
    """构造追加测试使用的 canonical record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload=payload or {"value": record_id},
    )


async def _created_journal(
    tmp_path: Path,
) -> tuple[JsonlSessionJournalCore, SessionLease]:
    """创建带 live lease 的 Journal。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor())
    return journal, created.lease


class _SlowAppendAdapter(DefaultSyncFileAdapter):
    """在 worker thread 内延迟 append，用于观察事件循环进展。"""

    def __init__(self, delay: float) -> None:
        """记录指定同步延迟。"""
        self.delay = delay
        self.started = threading.Event()

    def append_durable(self, path: Path, payload: bytes) -> None:
        """延迟后执行真实 durable append。"""
        self.started.set()
        time.sleep(self.delay)
        super().append_durable(path, payload)


class _FailOnceAppendAdapter(DefaultSyncFileAdapter):
    """第一次 append 注入 IO 失败，之后恢复真实文件行为。"""

    def __init__(self) -> None:
        """初始化单次故障开关。"""
        self.failed = False

    def append_durable(self, path: Path, payload: bytes) -> None:
        """第一次抛 OSError，后续正常提交。"""
        if not self.failed:
            self.failed = True
            raise OSError("injected append failure")
        super().append_durable(path, payload)


@pytest.mark.anyio
async def test_create_session_commits_three_initial_records(tmp_path: Path) -> None:
    """create 成功前必须 durable commit 固定顺序的三条初始化记录。"""
    journal = JsonlSessionJournalCore(tmp_path)

    created = await journal.create_session(_descriptor())
    records = [record async for record in journal.load("ses_1")]

    assert [record.record_type for record in records] == [
        "session_started",
        "thread_created",
        "thread_bound",
    ]
    assert [record.seq for record in records] == [1, 2, 3]
    assert created.ack.first_seq == 1
    assert created.ack.last_seq == 3
    assert created.ack.record_ids == tuple(record.record_id for record in records)


@pytest.mark.anyio
async def test_create_session_retry_reuses_same_live_result(tmp_path: Path) -> None:
    """同一实例中的完全相同 create 重试复用原 lease 与 ack。"""
    journal = JsonlSessionJournalCore(tmp_path)
    descriptor = _descriptor()

    first = await journal.create_session(descriptor)
    retried = await journal.create_session(descriptor)

    assert retried == first


@pytest.mark.anyio
async def test_existing_file_is_not_adopted_by_another_core(tmp_path: Path) -> None:
    """另一个 core 不得把已存在文件当成自己的 live Session。"""
    creator = JsonlSessionJournalCore(tmp_path)
    await creator.create_session(_descriptor())
    contender = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(JournalAlreadyExistsError):
        await contender.create_session(_descriptor())


@pytest.mark.anyio
async def test_live_session_rejects_a_different_creator(tmp_path: Path) -> None:
    """已有 live writer 时，不同 creation identity 必须收到 busy。"""
    journal = JsonlSessionJournalCore(tmp_path)
    await journal.create_session(_descriptor())

    with pytest.raises(JournalBusyError):
        await journal.create_session(_descriptor(creation_operation_id="create_2"))


@pytest.mark.anyio
@pytest.mark.parametrize("session_id", ["../escape", "nested/path", ".", "a" * 129])
async def test_create_session_rejects_unsafe_session_path(
    tmp_path: Path, session_id: str
) -> None:
    """session id 不能逃逸 root，也不能映射为特殊路径。"""
    journal = JsonlSessionJournalCore(tmp_path)

    with pytest.raises(ValueError, match="unsafe session_id"):
        await journal.create_session(_descriptor(session_id=session_id))


@pytest.mark.anyio
async def test_create_session_writes_one_complete_batch(tmp_path: Path) -> None:
    """初始化文件必须包含 BEGIN、三 envelope、COMMIT 五条完整行。"""
    journal = JsonlSessionJournalCore(tmp_path)

    await journal.create_session(_descriptor())

    path = tmp_path / "ses_1.journal.jsonl"
    physical = path.read_bytes()
    assert physical.endswith(b"\n")
    assert len(physical.splitlines()) == 5


@pytest.mark.anyio
async def test_create_session_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """durable create 必须依次覆盖文件内容与新目录项。"""
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", fsync_calls.append)
    journal = JsonlSessionJournalCore(tmp_path)

    await journal.create_session(_descriptor())

    assert len(fsync_calls) == 2


@pytest.mark.anyio
async def test_append_single_and_batch_return_durable_ranges(tmp_path: Path) -> None:
    """单条与批次追加分别返回连续的一个 durable ack。"""
    journal, lease = await _created_journal(tmp_path)

    single = await journal.append(_record(), lease=lease, expected_seq=3)
    batch = await journal.append_batch(
        (_record(record_id="rec_2"), _record(record_id="rec_3")),
        lease=lease,
        expected_seq=4,
    )

    assert (single.first_seq, single.last_seq) == (4, 4)
    assert (batch.first_seq, batch.last_seq) == (5, 6)
    assert batch.record_ids == ("rec_2", "rec_3")
    loaded = [item async for item in journal.load("ses_1", after_seq=3)]
    assert [item.record_id for item in loaded] == ["rec_1", "rec_2", "rec_3"]


@pytest.mark.anyio
async def test_ack_loss_retry_returns_original_ack_before_cas(tmp_path: Path) -> None:
    """相同 record 的重试先命中幂等索引，不受旧 expected_seq 影响。"""
    journal, lease = await _created_journal(tmp_path)
    record = _record()

    first = await journal.append(record, lease=lease, expected_seq=3)
    retried = await journal.append(record, lease=lease, expected_seq=3)

    assert retried == first


@pytest.mark.anyio
async def test_same_record_id_with_different_content_conflicts(tmp_path: Path) -> None:
    """record id 相同但完整 caller fingerprint 不同必须冲突。"""
    journal, lease = await _created_journal(tmp_path)
    await journal.append(
        _record(payload={"value": 1}),
        lease=lease,
        expected_seq=3,
    )

    with pytest.raises(JournalConflictError, match="record content conflict"):
        await journal.append(
            _record(payload={"value": 2}),
            lease=lease,
            expected_seq=3,
        )


@pytest.mark.anyio
async def test_append_batch_retry_requires_exact_original_batch(tmp_path: Path) -> None:
    """完整原批次可幂等重试，部分重叠或重组必须冲突。"""
    journal, lease = await _created_journal(tmp_path)
    records = (_record(record_id="rec_1"), _record(record_id="rec_2"))
    first = await journal.append_batch(records, lease=lease, expected_seq=3)

    retried = await journal.append_batch(records, lease=lease, expected_seq=3)
    assert retried == first
    with pytest.raises(JournalConflictError, match="batch idempotency conflict"):
        await journal.append_batch(
            (records[1], _record(record_id="rec_3")),
            lease=lease,
            expected_seq=3,
        )


@pytest.mark.anyio
async def test_append_rejects_stale_expected_seq(tmp_path: Path) -> None:
    """新 record 必须与当前 committed tail 做 compare-and-append。"""
    journal, lease = await _created_journal(tmp_path)

    with pytest.raises(JournalConflictError) as raised:
        await journal.append(_record(), lease=lease, expected_seq=2)

    assert raised.value.expected_seq == 2
    assert raised.value.actual_seq == 3


@pytest.mark.anyio
async def test_append_rejects_stale_lease_epoch(tmp_path: Path) -> None:
    """lease 任一 fencing 字段不匹配均不得写入。"""
    journal, lease = await _created_journal(tmp_path)
    stale = lease.model_copy(update={"writer_epoch": lease.writer_epoch + 1})

    with pytest.raises(JournalLeaseError):
        await journal.append(_record(), lease=stale, expected_seq=3)


@pytest.mark.anyio
async def test_pre_commit_cancellation_creates_no_file(tmp_path: Path) -> None:
    """进入文件变更前收到取消，不得产生任何 Session 文件。"""
    journal = JsonlSessionJournalCore(tmp_path)

    with anyio.CancelScope() as scope:
        scope.cancel()
        await journal.create_session(_descriptor())

    assert not (tmp_path / "ses_1.journal.jsonl").exists()


@pytest.mark.anyio
async def test_post_start_cancellation_is_shielded_to_ack(tmp_path: Path) -> None:
    """commit 已在线程中开始后，外部取消仍等待期限内的明确 ack。"""
    adapter = _SlowAppendAdapter(0.03)
    journal = JsonlSessionJournalCore(
        tmp_path,
        sync_file_adapter=adapter,
        commit_timeout=0.5,
    )
    created = await journal.create_session(_descriptor())
    results: list[JournalAck] = []
    scope = anyio.CancelScope()

    async def append_under_scope() -> None:
        """在可由测试触发的 cancel scope 内执行 append。"""
        with scope:
            results.append(
                await journal.append(
                    _record(), lease=created.lease, expected_seq=3
                )
            )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(append_under_scope)
        await anyio.to_thread.run_sync(adapter.started.wait)
        scope.cancel()

    assert len(results) == 1
    assert results[0].last_seq == 4


@pytest.mark.anyio
async def test_commit_deadline_freezes_writer_as_recovery_required(
    tmp_path: Path,
) -> None:
    """同步 commit 超过期限时必须有界返回并关闭普通追加。"""
    adapter = _SlowAppendAdapter(0.08)
    journal = JsonlSessionJournalCore(
        tmp_path,
        sync_file_adapter=adapter,
        commit_timeout=0.01,
    )
    created = await journal.create_session(_descriptor())

    with pytest.raises(JournalRecoveryRequiredError):
        await journal.append(_record(), lease=created.lease, expected_seq=3)
    with pytest.raises(JournalRecoveryRequiredError):
        await journal.append(
            _record(record_id="rec_2"),
            lease=created.lease,
            expected_seq=3,
        )
    await anyio.sleep(0.1)


@pytest.mark.anyio
async def test_injected_io_failure_returns_no_ack_or_tail_advance(
    tmp_path: Path,
) -> None:
    """明确 IO error 不得推进内存 tail，调用方可用同一 CAS 重试。"""
    adapter = _FailOnceAppendAdapter()
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())

    with pytest.raises(OSError, match="injected append failure"):
        await journal.append(_record(), lease=created.lease, expected_seq=3)
    retried = await journal.append(_record(), lease=created.lease, expected_seq=3)

    assert retried.first_seq == 4


@pytest.mark.anyio
async def test_slow_fsync_does_not_block_event_loop(tmp_path: Path) -> None:
    """同步慢 IO 必须在线程池执行，让独立 async ticker 持续运行。"""
    adapter = _SlowAppendAdapter(0.05)
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    stopped = anyio.Event()
    ticks = 0

    async def ticker() -> None:
        """记录 append 等待期间的事件循环调度次数。"""
        nonlocal ticks
        while not stopped.is_set():
            ticks += 1
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(ticker)
        await journal.append(_record(), lease=created.lease, expected_seq=3)
        stopped.set()

    assert ticks > 1


@pytest.mark.anyio
async def test_close_releases_live_lease_without_writing_session_end(
    tmp_path: Path,
) -> None:
    """close 只清理同进程 capability，不得伪造 session_ended。"""
    journal, lease = await _created_journal(tmp_path)

    await journal.close()

    with pytest.raises(JournalLeaseError):
        await journal.append(_record(), lease=lease, expected_seq=3)
    records = [item async for item in journal.load("ses_1")]
    assert [item.record_type for item in records] == [
        "session_started",
        "thread_created",
        "thread_bound",
    ]
