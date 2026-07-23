"""SessionAuditCoordinator finish 事实、reservation 与防御性结果测试。"""

from __future__ import annotations

from dataclasses import replace

import anyio
import pytest

import taifeng.loop.audit_lifecycle as audit_lifecycle
from taifeng.loop.audit import AuditHealth, SessionLifecycle
from tests.loop.test_audit_coordinator_lifecycle import (
    _coordinator,
    _LifecycleCore,
    _threads,
)


@pytest.mark.anyio
async def test_success_reports_terminal_audit_and_lease_release_independently() -> None:
    """terminal ack 与 close 都成功时，两项事实分别为真。"""
    coordinator = _coordinator(_LifecycleCore())

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")
    snapshot = coordinator.snapshot()

    assert result.audit_complete is True
    assert result.lease_released is True
    assert snapshot.audit_complete is True
    assert snapshot.lease_released is True
    assert snapshot.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_terminal_ack_survives_close_failure_as_complete_audit_fact() -> None:
    """close 失败只否定 lease release，不得推翻 durable terminal 事实。"""
    core = _LifecycleCore(close_failure=OSError("close-secret"))
    coordinator = _coordinator(core)

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")
    snapshot = coordinator.snapshot()

    assert result.audit_complete is True
    assert result.lease_released is False
    assert result.terminal_record_ids == tuple(
        record.record_id for record in core.append_calls[0].records
    )
    assert result.failure is not None
    assert snapshot.audit_complete is True
    assert snapshot.lease_released is False
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_terminal_failure_with_emergency_close_separates_both_facts() -> None:
    """terminal 未 ack 但 emergency close 成功时，审计不完整而 lease 已释放。"""
    coordinator = _coordinator(
        _LifecycleCore(append_failure=OSError("terminal-secret"))
    )

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")
    snapshot = coordinator.snapshot()

    assert result.audit_complete is False
    assert result.lease_released is True
    assert result.terminal_record_ids == ()
    assert snapshot.audit_complete is False
    assert snapshot.lease_released is True


@pytest.mark.anyio
async def test_terminal_and_emergency_close_failure_report_both_false() -> None:
    """terminal 与 emergency close 都失败时，两项事实都保持 false。"""
    coordinator = _coordinator(
        _LifecycleCore(
            append_failure=OSError("terminal-secret"),
            close_failure=RuntimeError("close-secret"),
        )
    )

    result = await coordinator.finish(thread_terminals=_threads(), reason="released")
    snapshot = coordinator.snapshot()

    assert result.audit_complete is False
    assert result.lease_released is False
    assert snapshot.audit_complete is False
    assert snapshot.lease_released is False


@pytest.mark.anyio
async def test_completed_reservations_are_pruned_before_each_new_admission() -> None:
    """长 Session 只跟踪 pending/accepted-incomplete work，不累积已完成历史。"""
    coordinator = _coordinator(_LifecycleCore())

    async def durable_accept() -> None:
        return None

    for index in range(100):
        completed = await coordinator.admit_work(
            f"sub_completed_{index}",
            durable_accept,
        )
        await completed.complete()
    pending = await coordinator.admit_work("sub_pending", durable_accept)

    assert coordinator.snapshot().accepted_work_ids == ("sub_pending",)
    assert len(coordinator._admissions) == 1  # noqa: SLF001
    await pending.complete()
    result = await coordinator.finish(thread_terminals=(), reason="released")
    assert result.audit_complete


@pytest.mark.anyio
async def test_finish_future_returns_defensive_values_for_every_caller() -> None:
    """caller 篡改 result 或嵌套 failure 不得污染 future canonical value。"""
    coordinator = _coordinator(
        _LifecycleCore(close_failure=OSError("close-secret"))
    )
    first = await coordinator.finish(thread_terminals=_threads(), reason="released")
    assert first._failure is not None  # noqa: SLF001
    object.__setattr__(first._failure, "code", "tampered")  # noqa: SLF001
    object.__setattr__(first, "terminal_record_ids", ("tampered",))

    second = await coordinator.finish(thread_terminals=_threads(), reason="released")

    assert second is not first
    assert second.failure is not None
    assert second.failure.code == "journal_io_error"
    assert second.terminal_record_ids != ("tampered",)


def test_lifecycle_synchronization_primitives_are_private() -> None:
    """模块只公开 DTO；可变 reservation/future 与 builder 使用内部命名。"""
    assert not hasattr(audit_lifecycle, "AdmissionReservation")
    assert not hasattr(audit_lifecycle, "FinishFuture")
    assert not hasattr(audit_lifecycle, "build_terminal_records")
    assert hasattr(audit_lifecycle, "_AdmissionReservation")
    assert hasattr(audit_lifecycle, "_FinishFuture")
    assert hasattr(audit_lifecycle, "_build_terminal_records")


def test_finish_contract_snapshot_starts_with_unknown_release_fact() -> None:
    """OPEN snapshot 在 finish 前不猜测审计或 lease release 结果。"""
    snapshot = _coordinator(_LifecycleCore()).snapshot()

    assert snapshot.lifecycle is SessionLifecycle.OPEN
    assert snapshot.audit_complete is None
    assert snapshot.lease_released is None


@pytest.mark.anyio
async def test_complete_immediately_retires_single_reservation() -> None:
    """work complete 返回时 snapshot 与内部 map 都不得保留 phantom in-flight。"""
    coordinator = _coordinator(_LifecycleCore())

    async def durable_accept() -> None:
        return None

    work = await coordinator.admit_work("done", durable_accept)
    await work.complete()

    assert coordinator.snapshot().accepted_work_ids == ()
    assert len(coordinator._admissions) == 0  # noqa: SLF001


@pytest.mark.anyio
async def test_idle_session_retires_one_hundred_completed_reservations() -> None:
    """无后续 admission/finish 的 idle Session 也不能积累 completed Event/reservation。"""
    coordinator = _coordinator(_LifecycleCore())

    async def durable_accept() -> None:
        return None

    for index in range(100):
        work = await coordinator.admit_work(f"done_{index}", durable_accept)
        await work.complete()

    assert coordinator.snapshot().accepted_work_ids == ()
    assert len(coordinator._admissions) == 0  # noqa: SLF001


class _ObservedCompletionEvent:
    """显式暴露 finish 已进入 completion wait 的测试 barrier。"""

    def __init__(self) -> None:
        """初始化真实 completion event 与 waiter barrier。"""
        self._completed = anyio.Event()
        self.wait_entered = anyio.Event()

    def is_set(self) -> bool:
        """代理 completion 状态。"""
        return self._completed.is_set()

    def set(self) -> None:
        """代理幂等完成通知。"""
        self._completed.set()

    async def wait(self) -> None:
        """先通知测试 finish 已持有快照，再等待完成。"""
        self.wait_entered.set()
        await self._completed.wait()


@pytest.mark.anyio
async def test_finish_snapshot_race_and_double_complete_have_no_lost_wakeup() -> None:
    """finish 已快照 reservation 时，complete 必须退休 map 并唤醒旧对象 waiter。"""
    coordinator = _coordinator(_LifecycleCore())

    async def durable_accept() -> None:
        return None

    work = await coordinator.admit_work("racing", durable_accept)
    observed = _ObservedCompletionEvent()
    object.__setattr__(work, "_completed", observed)
    finish_result = None

    async def finish() -> None:
        nonlocal finish_result
        finish_result = await coordinator.finish(
            thread_terminals=(),
            reason="released",
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish)
        await observed.wait_entered.wait()
        await work.complete()
        await work.complete()

    assert finish_result is not None and finish_result.audit_complete
    assert coordinator.snapshot().accepted_work_ids == ()
    assert len(coordinator._admissions) == 0  # noqa: SLF001


@pytest.mark.anyio
async def test_wrong_work_identity_cannot_retire_live_reservation() -> None:
    """持有同 callback 的错误 token 也不能退休 reservation 中的真实 work。"""
    coordinator = _coordinator(_LifecycleCore())

    async def durable_accept() -> None:
        return None

    work = await coordinator.admit_work("live", durable_accept)
    impostor = replace(work, _completed=anyio.Event())

    await impostor.complete()

    assert coordinator.snapshot().accepted_work_ids == ("live",)
    assert len(coordinator._admissions) == 1  # noqa: SLF001
    await work.complete()


@pytest.mark.anyio
async def test_late_complete_retires_unresolved_work_without_reviving_closed_session() -> None:
    """timeout/CLOSED 后真实 token 晚完成可清 introspection，但不得复活 lifecycle。"""
    coordinator = _coordinator(_LifecycleCore(), finish_timeout=0.01)

    async def durable_accept() -> None:
        return None

    work = await coordinator.admit_work("late", durable_accept)
    result = await coordinator.finish(thread_terminals=(), reason="released")
    assert not result.audit_complete
    assert coordinator.snapshot().accepted_work_ids == ("late",)

    await work.complete()

    snapshot = coordinator.snapshot()
    assert snapshot.lifecycle is SessionLifecycle.CLOSED
    assert snapshot.audit_complete is False
    assert snapshot.accepted_work_ids == ()
    assert len(coordinator._admissions) == 0  # noqa: SLF001
