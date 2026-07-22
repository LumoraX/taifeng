"""SessionJournal 单 Session writer 关闭行为测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    JournalLeaseError,
    JournalRecord,
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore

if TYPE_CHECKING:
    from pathlib import Path


def _descriptor(session_id: str) -> SessionDescriptor:
    """为指定 Session 构造独立初始化描述符。"""
    return SessionDescriptor(
        session_id=session_id,
        creation_operation_id=f"{session_id}:create",
        writer_id="worker_a",
        root_thread=RootThreadDescriptor(
            thread_id=f"{session_id}:root",
            entry_skill_id="general",
        ),
        config={"model": "sim"},
    )


def _record(session_id: str, record_id: str = "rec_1") -> JournalRecord:
    """构造属于指定 Session 的测试记录。"""
    return JournalRecord(
        session_id=session_id,
        record_id=record_id,
        record_type="test_record",
        actor=ActorRef(kind="user", source="test"),
        payload={"value": record_id},
    )


@pytest.mark.anyio
async def test_close_session_rejects_wrong_lease(tmp_path: Path) -> None:
    """错误 lease 不得关闭 writer，也不得影响正确 lease 后续追加。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor("ses_1"))
    forged = created.lease.model_copy(update={"lease_id": "forged"})

    with pytest.raises(JournalLeaseError):
        await journal.close_session(forged)

    ack = await journal.append(
        _record("ses_1"),
        lease=created.lease,
        expected_seq=3,
    )
    assert ack.last_seq == 4


@pytest.mark.anyio
async def test_close_session_removes_only_target_writer(tmp_path: Path) -> None:
    """关闭一个 Session 后，其他 Session writer 仍可正常提交。"""
    journal = JsonlSessionJournalCore(tmp_path)
    first = await journal.create_session(_descriptor("ses_1"))
    second = await journal.create_session(_descriptor("ses_2"))

    await journal.close_session(first.lease)

    with pytest.raises(JournalLeaseError):
        await journal.append(
            _record("ses_1"),
            lease=first.lease,
            expected_seq=3,
        )
    ack = await journal.append(
        _record("ses_2"),
        lease=second.lease,
        expected_seq=3,
    )
    assert ack.last_seq == 4


@pytest.mark.anyio
async def test_close_session_is_not_a_domain_record_and_repeat_is_rejected(
    tmp_path: Path,
) -> None:
    """单 Session close 只释放 capability；重复 close 收到稳定 lease 错误。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor("ses_1"))

    await journal.close_session(created.lease)
    with pytest.raises(JournalLeaseError):
        await journal.close_session(created.lease)

    records = [item async for item in journal.load("ses_1")]
    assert [item.record_type for item in records] == [
        "session_started",
        "thread_created",
        "thread_bound",
    ]


@pytest.mark.anyio
async def test_close_session_rejects_append_queued_after_close(
    tmp_path: Path,
) -> None:
    """close 先排队时，已取得旧 writer 引用的 append 也不得提交。"""
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor("ses_1"))
    writer = journal._writers["ses_1"]  # noqa: SLF001
    await writer.lock.acquire()
    outcomes: list[str] = []

    async def close_while_blocked() -> None:
        """先排队等待 writer lock。"""
        await journal.close_session(created.lease)
        outcomes.append("closed")

    async def append_while_blocked() -> None:
        """后排队并观察 writer 被 close 后的拒绝。"""
        try:
            await journal.append(
                _record("ses_1"),
                lease=created.lease,
                expected_seq=3,
            )
        except JournalLeaseError:
            outcomes.append("rejected")
        else:
            outcomes.append("committed")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(close_while_blocked)
        await anyio.lowlevel.checkpoint()
        task_group.start_soon(append_while_blocked)
        await anyio.lowlevel.checkpoint()
        writer.lock.release()

    assert outcomes == ["closed", "rejected"]
    records = [item async for item in journal.load("ses_1")]
    assert len(records) == 3
