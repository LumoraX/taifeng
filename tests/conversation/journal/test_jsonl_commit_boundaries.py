"""SessionJournal append 快照、mutation boundary 与 fencing 回归测试。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    JournalConflictError,
    JournalLeaseError,
    JournalRecord,
    JournalRecoveryRequiredError,
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.jsonl import (
    DefaultSyncFileAdapter,
    JsonlSessionJournalCore,
)

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JsonValue


def _descriptor() -> SessionDescriptor:
    """构造固定 Session 描述符。"""
    return SessionDescriptor(
        session_id="ses_1",
        creation_operation_id="create_1",
        writer_id="worker_a",
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
    """构造可区分 caller fingerprint 的记录。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload=payload or {"value": 1},
    )


class _BlockingAppendAdapter(DefaultSyncFileAdapter):
    """在实际 write 前阻塞，暴露 caller mutation 竞态窗口。"""

    def __init__(self) -> None:
        """初始化线程间同步事件。"""
        self.started = threading.Event()
        self.release = threading.Event()
        self.payload: bytes | None = None

    def append_durable(self, path: Path, payload: bytes) -> None:
        """捕获 precommit bytes，等待测试放行后再写入。"""
        self.payload = payload
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test release timed out")
        super().append_durable(path, payload)


class _UnknownFailureAdapter(DefaultSyncFileAdapter):
    """在 mutation 后抛出未分类异常，验证 core 保守冻结。"""

    def __init__(self, stage: Literal["partial", "complete"]) -> None:
        """记录注入阶段与 dispatch 次数。"""
        self.stage = stage
        self.append_calls = 0

    def append_durable(self, path: Path, payload: bytes) -> None:
        """写入部分或完整 batch 后抛出包含敏感文本的异常。"""
        self.append_calls += 1
        with path.open("ab") as stream:
            if self.stage == "partial":
                stream.write(payload.splitlines(keepends=True)[0])
                stream.flush()
                raise OSError("secret=/private/journal/path")
            stream.write(payload)
            stream.flush()
        raise RuntimeError("secret=provider-token")


class _BlockingUnknownFailureAdapter(DefaultSyncFileAdapter):
    """mutation 后阻塞并失败，用于覆盖取消与未知结果竞态。"""

    def __init__(self) -> None:
        """初始化线程同步事件与 dispatch 计数。"""
        self.started = threading.Event()
        self.release = threading.Event()
        self.append_calls = 0

    def append_durable(self, path: Path, payload: bytes) -> None:
        """写入 BEGIN 后等待取消，再抛出未分类 IO error。"""
        self.append_calls += 1
        with path.open("ab") as stream:
            stream.write(payload.splitlines(keepends=True)[0])
            stream.flush()
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test release timed out")
        raise OSError("secret=cancel-window")


class _CountingReadAdapter(DefaultSyncFileAdapter):
    """记录 strict scan 次数，证明无效 lease 不触碰存储。"""

    def __init__(self) -> None:
        """初始化读取计数。"""
        self.read_calls = 0

    def read_bytes(self, path: Path) -> bytes:
        """递增计数后执行真实读取。"""
        self.read_calls += 1
        return super().read_bytes(path)


@pytest.mark.anyio
async def test_append_uses_one_precommit_snapshot_for_disk_and_retries(
    tmp_path: Path,
) -> None:
    """IO 窗口修改 caller DTO，不得让磁盘事实与幂等索引分叉。"""
    adapter = _BlockingAppendAdapter()
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    caller_payload: dict[str, JsonValue] = {"nested": {"values": [1]}}
    mutable = JournalRecord.model_construct(
        session_id="ses_1",
        record_id="rec_1",
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload=caller_payload,
    )
    acknowledgements = []

    async def append_mutable() -> None:
        """提交绕过 Pydantic validator 的 caller-owned DTO。"""
        acknowledgements.append(
            await journal.append(mutable, lease=created.lease, expected_seq=3)
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(append_mutable)
        await anyio.to_thread.run_sync(adapter.started.wait)
        values = caller_payload["nested"]
        assert isinstance(values, dict)
        nested = values["values"]
        assert isinstance(nested, list)
        nested.append(2)
        adapter.release.set()

    loaded = [item async for item in journal.load("ses_1", after_seq=3)]
    assert loaded[0].payload == {"nested": {"values": [1]}}
    retry_v1 = _record(payload={"nested": {"values": [1]}})
    assert await journal.append(retry_v1, lease=created.lease, expected_seq=3) == (
        acknowledgements[0]
    )
    with pytest.raises(JournalConflictError, match="record content conflict"):
        await journal.append(
            _record(payload={"nested": {"values": [1, 2]}}),
            lease=created.lease,
            expected_seq=3,
        )


@pytest.mark.anyio
async def test_batch_snapshot_survives_mid_commit_nested_mutation(
    tmp_path: Path,
) -> None:
    """batch 提交中修改第二条嵌套 list，不得改变整批事实或索引。"""
    adapter = _BlockingAppendAdapter()
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    first_payload: dict[str, JsonValue] = {"nested": {"values": [1]}}
    second_payload: dict[str, JsonValue] = {"nested": {"values": [2]}}
    mutable_batch = (
        JournalRecord.model_construct(
            session_id="ses_1",
            record_id="rec_1",
            record_type="test_record",
            actor=ActorRef(kind="user", source="test"),
            payload=first_payload,
        ),
        JournalRecord.model_construct(
            session_id="ses_1",
            record_id="rec_2",
            record_type="test_record",
            actor=ActorRef(kind="user", source="test"),
            payload=second_payload,
        ),
    )
    acknowledgements = []

    async def append_mutable_batch() -> None:
        """提交包含 caller-owned nested list 的 batch。"""
        acknowledgements.append(
            await journal.append_batch(
                mutable_batch,
                lease=created.lease,
                expected_seq=3,
            )
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(append_mutable_batch)
        await anyio.to_thread.run_sync(adapter.started.wait)
        nested = second_payload["nested"]
        assert isinstance(nested, dict)
        values = nested["values"]
        assert isinstance(values, list)
        values.append(3)
        adapter.release.set()

    v1 = (
        _record(record_id="rec_1", payload={"nested": {"values": [1]}}),
        _record(record_id="rec_2", payload={"nested": {"values": [2]}}),
    )
    assert await journal.append_batch(
        v1,
        lease=created.lease,
        expected_seq=3,
    ) == acknowledgements[0]
    with pytest.raises(JournalConflictError, match="batch idempotency conflict"):
        await journal.append_batch(
            (
                v1[0],
                _record(
                    record_id="rec_2",
                    payload={"nested": {"values": [2, 3]}},
                ),
            ),
            lease=created.lease,
            expected_seq=3,
        )
    loaded = [item async for item in journal.load("ses_1", after_seq=3)]
    assert [item.payload for item in loaded] == [
        {"nested": {"values": [1]}},
        {"nested": {"values": [2]}},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["partial", "complete"])
async def test_unknown_post_dispatch_failure_freezes_writer_without_leaking_cause(
    tmp_path: Path,
    stage: Literal["partial", "complete"],
) -> None:
    """未明确证明 write 未开始的异常一律视为 ack unknown。"""
    adapter = _UnknownFailureAdapter(stage)
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())

    with pytest.raises(JournalRecoveryRequiredError) as first:
        await journal.append(_record(), lease=created.lease, expected_seq=3)
    with pytest.raises(JournalRecoveryRequiredError) as frozen:
        await journal.append(
            _record(record_id="rec_2"),
            lease=created.lease,
            expected_seq=3,
        )

    assert first.value.cause == "commit_outcome_unknown"
    assert frozen.value.cause == first.value.cause
    assert str(frozen.value) == str(first.value)
    assert "secret" not in str(first.value)
    assert first.value.__cause__ is None
    assert first.value.__context__ is None
    assert adapter.append_calls == 1


@pytest.mark.anyio
async def test_cancellation_after_mutation_then_failure_freezes_writer(
    tmp_path: Path,
) -> None:
    """mutation 后的 cancel 不能把随后 failure 降级成可重试 IO error。"""
    adapter = _BlockingUnknownFailureAdapter()
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    scope = anyio.CancelScope()
    errors: list[JournalRecoveryRequiredError] = []

    async def append_under_scope() -> None:
        """在外部可取消 scope 内捕获稳定恢复错误。"""
        with scope:
            try:
                await journal.append(_record(), lease=created.lease, expected_seq=3)
            except JournalRecoveryRequiredError as exc:
                errors.append(exc)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(append_under_scope)
        await anyio.to_thread.run_sync(adapter.started.wait)
        scope.cancel()
        adapter.release.set()

    assert len(errors) == 1
    assert errors[0].cause == "commit_outcome_unknown"
    assert errors[0].__cause__ is None
    assert errors[0].__context__ is None
    with pytest.raises(JournalRecoveryRequiredError) as frozen:
        await journal.append(
            _record(record_id="rec_2"),
            lease=created.lease,
            expected_seq=3,
        )
    assert frozen.value.cause == errors[0].cause
    assert adapter.append_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_id", "forged"),
        ("writer_id", "worker_b"),
        ("writer_epoch", 2),
        ("session_id", "ses_other"),
    ],
)
async def test_idempotent_retry_validates_full_lease_before_storage_access(
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    """伪造 capability 不得借幂等 retry 触发 scan 或取得原 ack。"""
    adapter = _CountingReadAdapter()
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    record = _record()
    await journal.append(record, lease=created.lease, expected_seq=3)
    reads_before_retry = adapter.read_calls
    forged = created.lease.model_copy(update={field: value})

    with pytest.raises(JournalLeaseError):
        await journal.append(record, lease=forged, expected_seq=3)

    assert adapter.read_calls == reads_before_retry


@pytest.mark.anyio
async def test_stale_lease_is_rejected_before_frozen_writer_state(tmp_path: Path) -> None:
    """writer 已冻结时仍先 fencing，不能向伪造 lease 暴露恢复状态。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor())
    writer = journal._writers["ses_1"]  # noqa: SLF001
    writer.recovery_required = True
    forged = created.lease.model_copy(update={"lease_id": "forged"})

    with pytest.raises(JournalLeaseError):
        await journal.append(_record(), lease=forged, expected_seq=3)


@pytest.mark.anyio
async def test_valid_retry_with_old_cas_still_returns_original_ack(tmp_path: Path) -> None:
    """合法 lease 的精确 retry 仍在 CAS 前返回原 ack。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor())
    record = _record()

    first = await journal.append(record, lease=created.lease, expected_seq=3)
    retried = await journal.append(record, lease=created.lease, expected_seq=3)

    assert retried == first


@pytest.mark.anyio
async def test_closed_lease_cannot_obtain_original_ack(tmp_path: Path) -> None:
    """close 后即使 record 完全相同也不得复用历史 ack。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor())
    record = _record()
    await journal.append(record, lease=created.lease, expected_seq=3)
    await journal.close_session(created.lease)

    with pytest.raises(JournalLeaseError):
        await journal.append(record, lease=created.lease, expected_seq=3)
