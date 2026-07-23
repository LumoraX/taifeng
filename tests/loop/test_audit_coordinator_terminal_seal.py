"""SessionAuditCoordinator terminal seal 与 fatal 传播回归测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    JournalAck,
    JournalRecord,
    RootThreadDescriptor,
    SessionDescriptor,
    SessionLease,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditCoordinator,
    SessionFinishingError,
    SessionLifecycle,
)
from tests.loop.test_audit_coordinator_lifecycle import (
    _coordinator,
    _LifecycleCore,
    _threads,
)

if TYPE_CHECKING:
    from pathlib import Path


def _record(
    record_id: str = "runtime_after_terminal",
    *,
    record_type: str = "test_record",
    thread_id: str | None = None,
) -> JournalRecord:
    """构造 terminal seal 边界使用的 runtime record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type=record_type,
        actor=ActorRef(kind="system", source="test"),
        payload={"payload_version": 1, "record_id": record_id},
        operation_id="op_runtime",
        thread_id=thread_id,
    )


class _PauseCloseJsonlCore(JsonlSessionJournalCore):
    """真实 JSONL core，仅在释放 lease 前提供确定性测试窗口。"""

    def __init__(self, root: Path) -> None:
        """初始化真实 core 与 close 两端事件。"""
        super().__init__(root)
        self.close_entered = anyio.Event()
        self.release_close = anyio.Event()

    async def close_session(self, lease: SessionLease) -> None:
        """暂停资源释放，但不改变真实 append/close 语义。"""
        self.close_entered.set()
        await self.release_close.wait()
        await super().close_session(lease)


class _PauseThreadTerminalCore(_LifecycleCore):
    """暂停普通 thread_terminal，让 finish 在其 ack 前形成去重快照。"""

    def __init__(self) -> None:
        """初始化普通 terminal append 的同步事件。"""
        super().__init__()
        self.thread_terminal_entered = anyio.Event()
        self.release_thread_terminal = anyio.Event()

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """仅暂停不含 session_ended 的 thread terminal batch。"""
        if (
            any(record.record_type == "thread_terminal" for record in records)
            and not any(record.record_type == "session_ended" for record in records)
        ):
            self.thread_terminal_entered.set()
            await self.release_thread_terminal.wait()
        return await super().append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )


@pytest.mark.anyio
async def test_real_jsonl_terminal_seal_keeps_session_ended_as_final_record(
    tmp_path: Path,
) -> None:
    """terminal durable ack 后即封口，close 等待期间不得再追加业务事实。"""
    core = _PauseCloseJsonlCore(tmp_path)
    created = await core.create_session(
        SessionDescriptor(
            session_id="ses_1",
            creation_operation_id="create_1",
            writer_id="writer_1",
            root_thread=RootThreadDescriptor(
                thread_id="thr_root",
                entry_skill_id="general",
            ),
            config={"model": "sim"},
        )
    )
    coordinator = SessionAuditCoordinator(
        core=core,
        lease=created.lease,
        expected_seq=created.ack.last_seq,
    )
    finish_result = None

    async def finish() -> None:
        nonlocal finish_result
        finish_result = await coordinator.finish(
            thread_terminals=(),
            reason="released",
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish)
        await core.close_entered.wait()
        try:
            with pytest.raises(SessionFinishingError):
                await coordinator.append(_record())
        finally:
            core.release_close.set()

    loaded = [item async for item in core.load("ses_1")]
    verification = await core.verify("ses_1")
    assert finish_result is not None and finish_result.audit_complete
    assert loaded[-1].record_type == "session_ended"
    assert verification.committed_tail_seq == loaded[-1].seq


@pytest.mark.anyio
async def test_closed_coordinator_rejects_append_without_freezing_or_dispatch() -> None:
    """正常 CLOSED 的 append 在 core 前拒绝，且不污染健康状态。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    await coordinator.finish(thread_terminals=(), reason="released")
    dispatched = len(core.append_calls)

    with pytest.raises(SessionFinishingError):
        await coordinator.append(_record())

    snapshot = coordinator.snapshot()
    assert len(core.append_calls) == dispatched
    assert snapshot.lifecycle is SessionLifecycle.CLOSED
    assert snapshot.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_finish_dedupes_thread_terminal_committed_while_waiting_append_lock() -> None:
    """finish 必须在 append lock 内读取最新 terminal thread 集合并原子封口。"""
    core = _PauseThreadTerminalCore()
    coordinator = _coordinator(core)
    ordinary = _record(
        "turn_1:thread_terminal:none:0",
        record_type="thread_terminal",
        thread_id="thr_a",
    )

    async def finish() -> None:
        await coordinator.finish(
            thread_terminals=_threads(),
            reason="released",
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(coordinator.append, ordinary)
        await core.thread_terminal_entered.wait()
        tasks.start_soon(finish)
        await anyio.lowlevel.checkpoint()
        core.release_thread_terminal.set()

    committed_thread_terminals = [
        record.thread_id
        for call in core.append_calls
        for record in call.records
        if record.record_type == "thread_terminal"
    ]
    assert committed_thread_terminals.count("thr_a") == 1
    assert core.append_calls[-1].records[-1].record_type == "session_ended"


@pytest.mark.anyio
async def test_terminal_append_fatal_settles_future_then_reraises_same_object() -> None:
    """terminal fatal 先 fail closed 并发布 follower 结果，再由 owner 原样重抛。"""
    fatal = SystemExit("terminal-fatal")
    emergency_fatal = KeyboardInterrupt()
    core = _LifecycleCore(
        append_failure=fatal,
        close_failure=emergency_fatal,
    )
    coordinator = _coordinator(core)

    with pytest.raises(SystemExit) as raised:
        await coordinator.finish(thread_terminals=_threads(), reason="released")

    with anyio.fail_after(1):
        follower = await coordinator.finish(
            thread_terminals=_threads(),
            reason="released",
        )
    assert raised.value is fatal
    assert not follower.audit_complete
    assert not follower.lease_released
    assert follower.failure is not None
    assert follower.failure.class_name == "SystemExit"
    assert coordinator.snapshot().lifecycle is SessionLifecycle.CLOSED


@pytest.mark.anyio
async def test_normal_close_fatal_settles_future_then_reraises_same_object() -> None:
    """terminal ack 后 close fatal 必须保留 ack、发布结果并由 owner 原样重抛。"""
    fatal = SystemExit("close-fatal")
    core = _LifecycleCore(close_failure=fatal)
    coordinator = _coordinator(core)

    with pytest.raises(SystemExit) as raised:
        await coordinator.finish(thread_terminals=_threads(), reason="released")

    with anyio.fail_after(1):
        follower = await coordinator.finish(
            thread_terminals=_threads(),
            reason="released",
        )
    assert raised.value is fatal
    assert follower.audit_complete
    assert not follower.lease_released
    assert follower.terminal_record_ids == tuple(
        record.record_id for record in core.append_calls[0].records
    )
    assert follower.failure is not None
    assert follower.failure.class_name == "SystemExit"


@pytest.mark.anyio
async def test_emergency_close_fatal_is_reraised_when_main_failure_is_not_fatal() -> None:
    """普通 terminal failure 后唯一 fatal 来自 emergency close 时仍须向 owner 传播。"""
    fatal = SystemExit("emergency-close-fatal")
    core = _LifecycleCore(
        append_failure=OSError("terminal-io"),
        close_failure=fatal,
    )
    coordinator = _coordinator(core)

    with pytest.raises(SystemExit) as raised:
        await coordinator.finish(thread_terminals=_threads(), reason="released")

    with anyio.fail_after(1):
        follower = await coordinator.finish(
            thread_terminals=_threads(),
            reason="released",
        )
    assert raised.value is fatal
    assert not follower.audit_complete
    assert follower.failure is not None
    assert follower.failure.class_name == "OSError"
