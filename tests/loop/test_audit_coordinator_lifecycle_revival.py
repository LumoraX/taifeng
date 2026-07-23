"""Session audit late acceptance 与 normal CLOSED effect gate 回归测试。"""

from __future__ import annotations

import anyio
import pytest

from taifeng.loop.audit import (
    AcceptedWork,
    AuditHealth,
    SessionAuditFrozenError,
    SessionFinishingError,
    SessionLifecycle,
)
from tests.loop.test_audit_coordinator_lifecycle import (
    _coordinator,
    _LifecycleCore,
)


@pytest.mark.anyio
async def test_successful_closed_session_is_healthy_but_effect_ineligible() -> None:
    """正常 CLOSED 不 freeze/root cancel，但 effect gate 必须稳定关闭。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)

    result = await coordinator.finish(thread_terminals=(), reason="released")

    assert result.audit_complete
    snapshot = coordinator.snapshot()
    assert snapshot.lifecycle is SessionLifecycle.CLOSED
    assert snapshot.health is AuditHealth.HEALTHY
    assert not snapshot.effect_gate_open
    assert not snapshot.root_cancelled
    with pytest.raises(SessionFinishingError):
        await coordinator.ensure_effect_allowed()


@pytest.mark.anyio
async def test_late_accept_during_frozen_finishing_cannot_return_enqueueable_work() -> None:
    """freeze 后 late durable ack 只保留证据，不得在 emergency close 窗口返回 token。"""
    core = _LifecycleCore(pause_close=True)
    coordinator = _coordinator(core, finish_timeout=0.1)
    accept_entered = anyio.Event()
    release_accept = anyio.Event()
    admission_done = anyio.Event()
    finish_done = anyio.Event()
    admitted_work: AcceptedWork | None = None
    admission_error: BaseException | None = None
    finish_result = None

    async def durable_accept() -> None:
        accept_entered.set()
        await release_accept.wait()

    async def admit() -> None:
        nonlocal admitted_work, admission_error
        try:
            admitted_work = await coordinator.admit_work(
                "sub_late_accept",
                durable_accept,
            )
        except BaseException as error:
            admission_error = error
        finally:
            admission_done.set()

    async def finish() -> None:
        nonlocal finish_result
        finish_result = await coordinator.finish(
            thread_terminals=(),
            reason="released",
        )
        finish_done.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(admit)
        await accept_entered.wait()
        tasks.start_soon(finish)
        with anyio.fail_after(1):
            await core.close_entered.wait()
        frozen = coordinator.snapshot()
        assert frozen.lifecycle is SessionLifecycle.FINISHING
        assert frozen.health is AuditHealth.RECOVERY_REQUIRED
        assert frozen.accepted_work_ids == ("sub_late_accept",)
        release_accept.set()
        await admission_done.wait()
        core.release_close.set()
        await finish_done.wait()

    assert admitted_work is None
    assert isinstance(admission_error, SessionAuditFrozenError)
    assert finish_result is not None
    assert not finish_result.audit_complete
    assert coordinator.snapshot().accepted_work_ids == ("sub_late_accept",)


@pytest.mark.anyio
async def test_late_accept_after_external_open_freeze_cannot_return_work() -> None:
    """OPEN callback 期间外部 freeze 后，settled acceptance 也不得返回 token。"""
    core = _LifecycleCore()
    coordinator = _coordinator(core)
    accept_entered = anyio.Event()
    release_accept = anyio.Event()
    admission_done = anyio.Event()
    admitted_work: AcceptedWork | None = None
    admission_error: BaseException | None = None

    async def durable_accept() -> None:
        accept_entered.set()
        await release_accept.wait()

    async def admit() -> None:
        nonlocal admitted_work, admission_error
        try:
            admitted_work = await coordinator.admit_work(
                "sub_open_freeze",
                durable_accept,
            )
        except BaseException as error:
            admission_error = error
        finally:
            admission_done.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(admit)
        await accept_entered.wait()
        coordinator.freeze(OSError("external-freeze"))
        release_accept.set()
        await admission_done.wait()

    assert admitted_work is None
    assert isinstance(admission_error, SessionAuditFrozenError)
    snapshot = coordinator.snapshot()
    assert snapshot.lifecycle is SessionLifecycle.OPEN
    assert snapshot.health is AuditHealth.RECOVERY_REQUIRED
    assert snapshot.accepted_work_ids == ("sub_open_freeze",)
