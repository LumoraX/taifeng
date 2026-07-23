"""SessionAuditCoordinator finish 事实、reservation 与防御性结果测试。"""

from __future__ import annotations

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
        completed.complete()
    pending = await coordinator.admit_work("sub_pending", durable_accept)

    assert coordinator.snapshot().accepted_work_ids == ("sub_pending",)
    assert len(coordinator._admissions) == 1  # noqa: SLF001
    pending.complete()
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
