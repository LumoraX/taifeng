"""SessionJournal create/append 的 commit outcome 分类测试。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Literal

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    CommitNotStartedError,
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


def _record(*, record_id: str = "rec_1") -> JournalRecord:
    """构造固定追加记录。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload={"value": record_id},
    )


def _fatal(kind: Literal["keyboard", "system"]) -> KeyboardInterrupt | SystemExit:
    """构造带敏感文本的 fatal 异常。"""
    if kind == "keyboard":
        return KeyboardInterrupt("secret=keyboard")
    return SystemExit("secret=system")


class _AppendFatalOnceAdapter(DefaultSyncFileAdapter):
    """第一次 append 在 prewrite 或 mutation 后抛 fatal。"""

    def __init__(
        self,
        kind: Literal["keyboard", "system"],
        *,
        prewrite: bool,
    ) -> None:
        """记录 fatal 类型与 mutation 边界。"""
        self.kind = kind
        self.prewrite = prewrite
        self.append_calls = 0

    def append_durable(self, path: Path, payload: bytes) -> None:
        """按测试边界抛 fatal，第二次恢复真实提交。"""
        self.append_calls += 1
        if self.append_calls > 1:
            super().append_durable(path, payload)
            return
        error = _fatal(self.kind)
        if self.prewrite:
            raise CommitNotStartedError(error)
        with path.open("ab") as stream:
            stream.write(payload.splitlines(keepends=True)[0])
            stream.flush()
        raise error


class _CreateMutationFailureAdapter(DefaultSyncFileAdapter):
    """create mutation 后抛普通、取消或 fatal 异常。"""

    def __init__(
        self,
        stage: Literal["partial", "complete"],
        error_kind: Literal["oserror", "runtime", "cancel", "fatal"],
    ) -> None:
        """记录注入阶段、异常类别和 dispatch 次数。"""
        self.stage = stage
        self.error_kind = error_kind
        self.create_calls = 0

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """落下部分或完整初始化 batch 后抛指定异常。"""
        self.create_calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            written = (
                payload.splitlines(keepends=True)[0]
                if self.stage == "partial"
                else payload
            )
            stream.write(written)
            stream.flush()
        if self.error_kind == "oserror":
            raise OSError("secret=create-path")
        if self.error_kind == "runtime":
            raise RuntimeError("secret=create-provider")
        if self.error_kind == "cancel":
            raise asyncio.CancelledError("secret=create-cancel")
        raise KeyboardInterrupt("secret=create-fatal")


class _PrewriteCreateOnceAdapter(DefaultSyncFileAdapter):
    """第一次 create 明确证明未 mutation，第二次真实创建。"""

    def __init__(self, error: BaseException) -> None:
        """保存要由 SPI marker 包装的原错误。"""
        self.error = error
        self.create_calls = 0

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """第一次抛 prewrite marker，后续执行默认适配器。"""
        self.create_calls += 1
        if self.create_calls == 1:
            raise CommitNotStartedError(self.error)
        super().create_exclusive(path, payload)


class _SlowCreateAfterMutationAdapter(DefaultSyncFileAdapter):
    """create 写入 BEGIN 后超过 core deadline。"""

    def __init__(self) -> None:
        """初始化 dispatch 计数。"""
        self.create_calls = 0

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """先 mutation 再阻塞，模拟结果未知的 timeout。"""
        self.create_calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload.splitlines(keepends=True)[0])
            stream.flush()
            time.sleep(0.08)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["keyboard", "system"])
async def test_append_post_dispatch_fatal_freezes_then_reraises_original(
    tmp_path: Path,
    kind: Literal["keyboard", "system"],
) -> None:
    """fatal 不得被吞成 Recovery，但 writer 必须先冻结。"""
    adapter = _AppendFatalOnceAdapter(kind, prewrite=False)
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())
    fatal_type = KeyboardInterrupt if kind == "keyboard" else SystemExit

    with pytest.raises(fatal_type):
        await journal.append(_record(), lease=created.lease, expected_seq=3)
    with pytest.raises(JournalRecoveryRequiredError) as frozen:
        await journal.append(
            _record(record_id="rec_2"),
            lease=created.lease,
            expected_seq=3,
        )

    assert frozen.value.cause == "commit_outcome_unknown"
    assert adapter.append_calls == 1


@pytest.mark.anyio
async def test_append_prewrite_fatal_reraises_without_freezing(tmp_path: Path) -> None:
    """SPI 明确未 mutation 的 fatal 可原样重抛并安全重试。"""
    adapter = _AppendFatalOnceAdapter("keyboard", prewrite=True)
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)
    created = await journal.create_session(_descriptor())

    with pytest.raises(KeyboardInterrupt, match="secret=keyboard") as raised:
        await journal.append(_record(), lease=created.lease, expected_seq=3)
    ack = await journal.append(_record(), lease=created.lease, expected_seq=3)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert ack.last_seq == 4
    assert adapter.append_calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stage", "error_kind"),
    [
        ("partial", "oserror"),
        ("complete", "runtime"),
        ("partial", "cancel"),
    ],
)
async def test_create_post_dispatch_nonfatal_requires_recovery_without_secret(
    tmp_path: Path,
    stage: Literal["partial", "complete"],
    error_kind: Literal["oserror", "runtime", "cancel"],
) -> None:
    """create mutation 后任何 nonfatal 结果未知都必须脱敏冻结 Session。"""
    adapter = _CreateMutationFailureAdapter(stage, error_kind)
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)

    with pytest.raises(JournalRecoveryRequiredError) as first:
        await journal.create_session(_descriptor())
    with pytest.raises(JournalRecoveryRequiredError) as repeated:
        await journal.create_session(_descriptor())

    assert first.value.cause == "commit_outcome_unknown"
    assert repeated.value.cause == first.value.cause
    assert str(repeated.value) == str(first.value)
    assert "secret" not in str(first.value)
    assert first.value.__cause__ is None
    assert first.value.__context__ is None
    assert adapter.create_calls == 1


@pytest.mark.anyio
async def test_create_timeout_after_mutation_freezes_repeated_create(
    tmp_path: Path,
) -> None:
    """create deadline 后不能把相同 operation 当作安全重试。"""
    adapter = _SlowCreateAfterMutationAdapter()
    journal = JsonlSessionJournalCore(
        tmp_path,
        sync_file_adapter=adapter,
        commit_timeout=0.01,
    )

    with pytest.raises(JournalRecoveryRequiredError) as first:
        await journal.create_session(_descriptor())
    with pytest.raises(JournalRecoveryRequiredError) as repeated:
        await journal.create_session(_descriptor())

    assert first.value.cause == "commit_outcome_unknown"
    assert repeated.value.cause == first.value.cause
    assert adapter.create_calls == 1
    await anyio.sleep(0.1)


@pytest.mark.anyio
async def test_create_prewrite_io_marker_is_unwrapped_and_retryable(
    tmp_path: Path,
) -> None:
    """CommitNotStartedError 是 SPI marker，core 只能暴露原 prewrite IO。"""
    adapter = _PrewriteCreateOnceAdapter(OSError("explicit prewrite failure"))
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)

    with pytest.raises(OSError, match="explicit prewrite failure") as raised:
        await journal.create_session(_descriptor())
    created = await journal.create_session(_descriptor())

    assert not isinstance(raised.value, CommitNotStartedError)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert created.ack.last_seq == 3
    assert adapter.create_calls == 2


@pytest.mark.anyio
async def test_create_post_dispatch_fatal_marks_session_before_reraising(
    tmp_path: Path,
) -> None:
    """create fatal 原样重抛，但随后相同 Session 必须要求恢复。"""
    adapter = _CreateMutationFailureAdapter("partial", "fatal")
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)

    with pytest.raises(KeyboardInterrupt, match="secret=create-fatal"):
        await journal.create_session(_descriptor())
    with pytest.raises(JournalRecoveryRequiredError) as repeated:
        await journal.create_session(_descriptor())

    assert repeated.value.cause == "commit_outcome_unknown"
    assert "secret" not in str(repeated.value)
    assert repeated.value.__cause__ is None
    assert repeated.value.__context__ is None
    assert adapter.create_calls == 1


@pytest.mark.anyio
async def test_create_prewrite_fatal_does_not_mark_session_recovery(
    tmp_path: Path,
) -> None:
    """明确未 mutation 的 create fatal 不得污染后续安全重试。"""
    adapter = _PrewriteCreateOnceAdapter(KeyboardInterrupt("secret=prewrite-fatal"))
    journal = JsonlSessionJournalCore(tmp_path, sync_file_adapter=adapter)

    with pytest.raises(KeyboardInterrupt, match="secret=prewrite-fatal") as raised:
        await journal.create_session(_descriptor())
    created = await journal.create_session(_descriptor())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert created.ack.last_seq == 3
    assert adapter.create_calls == 2
