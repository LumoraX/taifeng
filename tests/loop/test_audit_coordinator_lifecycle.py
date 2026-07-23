"""SessionAuditCoordinator admission 与幂等终结生命周期测试。"""

from __future__ import annotations

from dataclasses import dataclass

import anyio
import pytest

from taifeng.conversation.journal import (
    ActorRef,
    Durability,
    JournalAck,
    JournalRecord,
    SessionLease,
)
from taifeng.loop.audit import (
    AcceptedWork,
    SessionAuditCoordinator,
    SessionAuditFrozenError,
    SessionFinishingError,
    SessionLifecycle,
    ThreadTerminalRequest,
)

_ZERO_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class _AppendCall:
    """一次原子 append 的只读测试快照。"""

    records: tuple[JournalRecord, ...]
    expected_seq: int


class _LifecycleCore:
    """支持暂停与失败注入的最小可控 Journal core。"""

    def __init__(
        self,
        *,
        pause_terminal: bool = False,
        append_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        """配置 terminal append/close 的确定性边界行为。"""
        self.pause_terminal = pause_terminal
        self.append_failure = append_failure
        self.close_failure = close_failure
        self.append_calls: list[_AppendCall] = []
        self.close_calls: list[SessionLease] = []
        self.terminal_entered = anyio.Event()
        self.release_terminal = anyio.Event()

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """记录 batch，按配置暂停或失败，否则返回覆盖 batch 的 durable ack。"""
        self.append_calls.append(_AppendCall(records=records, expected_seq=expected_seq))
        if any(record.record_type == "session_ended" for record in records):
            self.terminal_entered.set()
            if self.pause_terminal:
                await self.release_terminal.wait()
            if self.append_failure is not None:
                raise self.append_failure
        return JournalAck(
            session_id=lease.session_id,
            first_seq=expected_seq + 1,
            last_seq=expected_seq + len(records),
            record_ids=tuple(record.record_id for record in records),
            tail_hash=_ZERO_HASH,
            writer_epoch=lease.writer_epoch,
            durability=Durability.COMMITTED,
        )

    async def close_session(self, lease: SessionLease) -> None:
        """记录 per-Session close，并按配置抛稳定测试异常。"""
        self.close_calls.append(lease)
        if self.close_failure is not None:
            raise self.close_failure


def _lease(session_id: str = "ses_1") -> SessionLease:
    """构造固定 live lease。"""
    return SessionLease(
        session_id=session_id,
        writer_id=f"writer_{session_id}",
        writer_epoch=1,
        lease_id=f"lease_{session_id}",
    )


def _coordinator(
    core: _LifecycleCore,
    *,
    session_id: str = "ses_1",
    finish_timeout: float = 1.0,
) -> SessionAuditCoordinator:
    """从初始化 batch 尾 seq=3 构造生命周期协调器。"""
    return SessionAuditCoordinator(
        core=core,
        lease=_lease(session_id),
        expected_seq=3,
        finish_timeout=finish_timeout,
    )


def _threads() -> tuple[ThreadTerminalRequest, ...]:
    """故意以非字典序返回两个待终结 thread。"""
    return (
        ThreadTerminalRequest(
            thread_id="thr_z",
            status="complete",
            end_reason="finished",
        ),
        ThreadTerminalRequest(
            thread_id="thr_a",
            status="cancelled",
            end_reason="session_finished",
        ),
    )


@pytest.mark.anyio
async def test_finish_snapshots_durable_accepted_work_and_closes_admission() -> None:
    """FINISHING 必须等待胜者快照内 work，且稳定拒绝后续 admission。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    accepted = False

    async def durable_accept() -> None:
        nonlocal accepted
        accepted = True

    initial = coordinator.snapshot()
    assert initial.lifecycle is SessionLifecycle.OPEN
    assert initial.audit_complete is None
    assert initial.accepted_work_ids == ()

    completed_work = await coordinator.admit_work("sub_completed", durable_accept)
    completed_work.complete()
    work = await coordinator.admit_work("sub_in_flight", durable_accept)
    assert accepted
    assert coordinator.snapshot().lifecycle is SessionLifecycle.OPEN

    result = None

    async def finish() -> None:
        nonlocal result
        result = await coordinator.finish(thread_terminals=_threads(), reason="released")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish)
        for _ in range(20):
            if coordinator.snapshot().lifecycle is SessionLifecycle.FINISHING:
                break
            await anyio.lowlevel.checkpoint()

        snapshot = coordinator.snapshot()
        assert snapshot.lifecycle is SessionLifecycle.FINISHING
        assert snapshot.accepted_work_ids == ("sub_completed", "sub_in_flight")
        assert not core.append_calls

        rejected_called = False

        async def rejected_accept() -> None:
            nonlocal rejected_called
            rejected_called = True

        with pytest.raises(SessionFinishingError):
            await coordinator.admit_work("sub_2", rejected_accept)
        assert not rejected_called
        work.complete()

    assert result is not None
    assert result.audit_complete
    assert coordinator.snapshot().lifecycle is SessionLifecycle.CLOSED
    assert coordinator.snapshot().audit_complete is True

    accepted = False
    with pytest.raises(SessionFinishingError):
        await coordinator.admit_work("sub_after_close", durable_accept)
    assert not accepted


@pytest.mark.anyio
async def test_finish_cannot_cut_between_durable_accept_and_work_registration() -> None:
    """shared lock 必须让已开始的 durable acceptance 完整进入 finish 快照。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    accept_entered = anyio.Event()
    release_accept = anyio.Event()
    admitted = anyio.Event()
    accepted_work: AcceptedWork | None = None

    async def durable_accept() -> None:
        accept_entered.set()
        await release_accept.wait()

    async def admit() -> None:
        nonlocal accepted_work
        accepted_work = await coordinator.admit_work("sub_racing", durable_accept)
        admitted.set()

    async def finish() -> None:
        await coordinator.finish(thread_terminals=(), reason="released")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(admit)
        await accept_entered.wait()
        tasks.start_soon(finish)
        await anyio.lowlevel.checkpoint()
        assert coordinator.snapshot().lifecycle is SessionLifecycle.OPEN
        release_accept.set()
        await admitted.wait()
        for _ in range(20):
            if coordinator.snapshot().lifecycle is SessionLifecycle.FINISHING:
                break
            await anyio.lowlevel.checkpoint()
        assert coordinator.snapshot().accepted_work_ids == ("sub_racing",)
        assert accepted_work is not None
        accepted_work.complete()

    assert coordinator.snapshot().audit_complete is True
    assert core.close_calls == [_lease()]


@pytest.mark.anyio
async def test_concurrent_finish_callers_share_result_terminal_batch_and_close() -> None:
    """并发与成功后的 finish 必须复用同一结果，且只 append/close 一次。"""
    core = _LifecycleCore(pause_terminal=True)
    coordinator = _coordinator(core)
    results = []

    async def finish() -> None:
        results.append(
            await coordinator.finish(thread_terminals=_threads(), reason="released")
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish)
        await core.terminal_entered.wait()
        tasks.start_soon(finish)
        await anyio.lowlevel.checkpoint()
        core.release_terminal.set()

    later = await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert len(results) == 2
    assert results[0] is results[1] is later
    assert len(core.append_calls) == 1
    assert core.close_calls == [_lease()]

    terminal = core.append_calls[0].records
    assert [record.record_type for record in terminal] == [
        "thread_terminal",
        "thread_terminal",
        "session_ended",
    ]
    assert [record.thread_id for record in terminal[:-1]] == ["thr_a", "thr_z"]
    assert [record.record_id for record in terminal] == [
        "ses_1:lifecycle:end:thread_terminal:none:0",
        "ses_1:lifecycle:end:thread_terminal:none:1",
        "ses_1:lifecycle:end:session_ended:none:0",
    ]
    assert terminal[-1].payload["audit_complete"] is True


@pytest.mark.anyio
async def test_already_committed_thread_terminal_is_not_duplicated_at_finish() -> None:
    """此前 durable ack 的 thread_terminal 不得在 Session terminal batch 重写。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    record = JournalRecord(
        session_id="ses_1",
        record_id="turn_1:thread_terminal:none:0",
        record_type="thread_terminal",
        actor=ActorRef(kind="system", source="test"),
        payload={"payload_version": 1, "status": "complete", "end_reason": "finished"},
        operation_id="turn_1",
        thread_id="thr_a",
    )
    await coordinator.append(record)

    await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert len(core.append_calls) == 2
    terminal = core.append_calls[-1].records
    assert [item.thread_id for item in terminal if item.record_type == "thread_terminal"] == [
        "thr_z"
    ]
    assert terminal[0].record_id == "ses_1:lifecycle:end:thread_terminal:none:0"


@pytest.mark.anyio
async def test_terminal_failure_emergency_closes_and_all_callers_see_incomplete() -> None:
    """terminal commit 失败仍单次 close，且共享结果绝不伪装为成功。"""
    core = _LifecycleCore(
        pause_terminal=True,
        append_failure=OSError("sensitive-terminal-detail"),
    )
    coordinator = _coordinator(core)
    results = []

    async def finish() -> None:
        results.append(
            await coordinator.finish(thread_terminals=_threads(), reason="released")
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish)
        await core.terminal_entered.wait()
        tasks.start_soon(finish)
        core.release_terminal.set()

    assert results[0] is results[1]
    assert not results[0].audit_complete
    assert results[0].terminal_record_ids == ()
    assert results[0].failure is not None
    assert results[0].failure.code == "journal_io_error"
    assert "sensitive-terminal-detail" not in str(results[0])
    assert core.close_calls == [_lease()]
    snapshot = coordinator.snapshot()
    assert snapshot.lifecycle is SessionLifecycle.CLOSED
    assert snapshot.audit_complete is False


@pytest.mark.anyio
async def test_emergency_close_failure_does_not_replace_first_stable_failure() -> None:
    """emergency close 异常不得覆盖首因或泄漏任意 repr。"""
    core = _LifecycleCore(
        append_failure=OSError("terminal-secret"),
        close_failure=RuntimeError("close-secret at 0xdeadbeef"),
    )
    coordinator = _coordinator(core)

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert not result.audit_complete
    assert result.failure is not None
    assert result.failure.code == "journal_io_error"
    assert "terminal-secret" not in str(result)
    assert "close-secret" not in str(result)
    assert coordinator.snapshot().first_failure == result.failure
    assert core.close_calls == [_lease()]


@pytest.mark.anyio
async def test_frozen_coordinator_finishes_fail_closed_without_terminal_append() -> None:
    """freeze 与 lifecycle 交互必须直接 emergency close，不伪造终结记录。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    frozen = coordinator.freeze(OSError("journal-down"))

    async def should_not_accept() -> None:
        raise AssertionError("frozen Session must not call acceptance")

    with pytest.raises(SessionAuditFrozenError) as raised:
        await coordinator.admit_work("sub_1", should_not_accept)
    assert raised.value is frozen

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert not result.audit_complete
    assert not core.append_calls
    assert core.close_calls == [_lease()]
    assert coordinator.snapshot().lifecycle is SessionLifecycle.CLOSED


@pytest.mark.anyio
async def test_finish_owner_cancellation_cannot_strand_shared_future() -> None:
    """owner 被取消时，bounded shield 仍须完成共享终结 future。"""
    core = _LifecycleCore(pause_terminal=True)
    coordinator = _coordinator(core)
    owner_done = anyio.Event()

    async def owner() -> None:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await coordinator.finish(thread_terminals=_threads(), reason="released")
        owner_done.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(owner)
        await core.terminal_entered.wait()
        core.release_terminal.set()
        with anyio.fail_after(1):
            result = await coordinator.finish(
                thread_terminals=_threads(),
                reason="released",
            )
        await owner_done.wait()

    assert result.audit_complete
    assert coordinator.snapshot().lifecycle is SessionLifecycle.CLOSED
    assert len(core.append_calls) == 1
    assert core.close_calls == [_lease()]


@pytest.mark.anyio
async def test_unfinished_accepted_work_times_out_to_incomplete_emergency_close() -> None:
    """accepted work 永不收敛时，bounded finish 不得永久停在 FINISHING。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core, finish_timeout=0.01)

    async def durable_accept() -> None:
        return None

    await coordinator.admit_work("sub_never_done", durable_accept)

    with anyio.fail_after(1):
        result = await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert not result.audit_complete
    assert not core.append_calls
    assert core.close_calls == [_lease()]
    assert coordinator.snapshot().lifecycle is SessionLifecycle.CLOSED


@pytest.mark.anyio
async def test_finishing_one_session_does_not_mutate_another_session() -> None:
    """一个 Session 的 accepted work 不得阻塞或关闭另一个 Session。"""
    first_core = _LifecycleCore()
    second_core = _LifecycleCore()
    first = _coordinator(first_core, session_id="ses_1")
    second = _coordinator(second_core, session_id="ses_2")

    async def durable_accept() -> None:
        return None

    work = await first.admit_work("sub_1", durable_accept)

    async with anyio.create_task_group() as tasks:
        async def finish_first() -> None:
            await first.finish(thread_terminals=_threads(), reason="released")

        tasks.start_soon(finish_first)
        for _ in range(20):
            if first.snapshot().lifecycle is SessionLifecycle.FINISHING:
                break
            await anyio.lowlevel.checkpoint()
        second_result = await second.finish(thread_terminals=(), reason="released")
        work.complete()

    assert second_result.audit_complete
    assert second.snapshot().lifecycle is SessionLifecycle.CLOSED
    assert first.snapshot().lifecycle is SessionLifecycle.CLOSED
    assert first_core.close_calls == [_lease("ses_1")]
    assert second_core.close_calls == [_lease("ses_2")]
