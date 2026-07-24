"""CancelTurn lifecycle 与 cancellation-origin 审查回归。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal import SubmissionAppliedV1
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit import (
    AuditHealth,
    SessionAuditFrozenError,
    SessionFinishingError,
    SessionLifecycle,
)
from taifeng.loop.audit_targets import TargetCancellationRegistry
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CancelTurn, UserMessage
from tests.loop.test_audit_cancel_turn import (
    _controlled_sim_client,
    _load_journal,
    _stop_actor,
    _wait_for_record,
    _wait_for_requests,
)
from tests.loop.test_audit_coordinator import _coordinator, _RecordingCore
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from pathlib import Path


def _held_sim_client() -> SimClient:
    """用 reviewed Sim signal 保持取消后的 target operation。"""
    return SimClient(
        turns=[SimTurn(text="unused", await_signal="release-target")],
    )


@pytest.mark.anyio
async def test_pending_cancel_releases_admission_lock_for_shutdown(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelTurn pending/raw-cancel 时 shutdown 仍可关闭 intake 并入队。"""
    client = _held_sim_client()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    cancel_task: asyncio.Task[str] | None = None
    try:
        target_id = await engine.submit(UserMessage(text="held-cancel"))
        await _wait_for_requests(client, 1)
        target_token = engine._pending[target_id].cancel  # noqa: SLF001
        monkeypatch.setattr(
            "taifeng.loop.submission.secrets.token_hex",
            lambda _: "lockcancel",
        )
        cancel_task = asyncio.create_task(
            engine.submit(CancelTurn(submission_id=target_id))
        )
        with anyio.fail_after(2):
            await target_token.wait_cancelled()
        cancel_task.cancel("caller cancelled while target pending")

        with anyio.fail_after(0.2):
            await engine.shutdown()
        assert coordinator.snapshot().lifecycle is SessionLifecycle.FINISHING
        assert engine._audited_shutdown_enqueued  # noqa: SLF001

        client.coordinator.signal("release-target")
        with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
            await cancel_task
        await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id="sub_lockcancel",
        )
        assert coordinator.snapshot().accepted_work_ids == ()
        with anyio.fail_after(2):
            await actor
    finally:
        client.coordinator.signal("release-target")
        if cancel_task is not None and not cancel_task.done():
            with suppress(asyncio.CancelledError):
                await cancel_task
        await _stop_actor(actor)


@pytest.mark.anyio
async def test_audited_shutdown_cancels_session_root_and_active_target(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """actor shutdown 取消 coordinator root/target subtree 且不伪造 CancelTurn。"""
    client = _controlled_sim_client()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    target_id = await engine.submit(UserMessage(text="cancel-me"))
    await _wait_for_requests(client, 1)
    target_token = engine._pending[target_id].cancel  # noqa: SLF001
    child = target_token.child("child-effect")

    await engine.shutdown()
    with anyio.fail_after(2):
        await target_token.wait_cancelled()
    client.coordinator.signal("release-target")
    with anyio.fail_after(0.2):
        await actor

    assert coordinator.session_root_cancel.is_cancelled
    assert target_token.is_cancelled
    assert child.is_cancelled
    records = await _load_journal(core)
    assert not any(
        envelope.record_type == "turn_cancelled"
        and envelope.submission_id == target_id
        for envelope in records
    )


@pytest.mark.anyio
async def test_registry_rejects_same_cancel_id_for_different_target() -> None:
    """registry 不得把 cancel identity 的旧结果复用于另一个 target。"""
    registry = TargetCancellationRegistry(CancellationToken(name="session-root"))
    first = registry.register("sub_first")
    second = registry.register("sub_second")
    pending = asyncio.create_task(
        registry.resolve(
            cancel_submission_id="sub_cancel",
            target_submission_id="sub_first",
        )
    )
    with anyio.fail_after(1):
        await first.wait_cancelled()
    assert registry.register_terminal(
        "sub_first",
        first,
        terminal_record_ids=("turn:first:cancelled",),
    )
    await pending

    with pytest.raises(RuntimeError) as caught:
        await registry.resolve(
            cancel_submission_id="sub_cancel",
            target_submission_id="sub_second",
        )
    assert type(caught.value).__name__ == "TargetCancelIdentityConflictError"
    assert not second.is_cancelled


@pytest.mark.anyio
async def test_coordinator_freezes_on_cancel_identity_target_conflict() -> None:
    """coordinator 把 cancel id/target 冲突视为 fail-closed invariant。"""
    coordinator = _coordinator(_RecordingCore())
    first = coordinator.register_target("sub_first")
    coordinator.register_target("sub_second")
    pending = asyncio.create_task(
        coordinator.resolve_target_cancel(
            cancel_submission_id="sub_cancel",
            target_submission_id="sub_first",
        )
    )
    with anyio.fail_after(1):
        await first.wait_cancelled()
    assert coordinator.register_target_terminal(
        "sub_first",
        first,
        terminal_record_ids=("turn:first:cancelled",),
    )
    await pending

    with pytest.raises(SessionAuditFrozenError):
        await coordinator.resolve_target_cancel(
            cancel_submission_id="sub_cancel",
            target_submission_id="sub_second",
        )
    assert coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_closed_rejects_healthy_cancel_before_accept_or_enqueue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """healthy CLOSED 不是 frozen safe-degrade，必须保持 lifecycle 拒绝。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    result = await coordinator.finish(thread_terminals=(), reason="released")
    assert result.audit_complete
    assert coordinator.health is AuditHealth.HEALTHY
    before = await _load_journal(core)

    with pytest.raises(SessionFinishingError):
        await engine.submit(CancelTurn(submission_id="sub_target"))

    assert await _load_journal(core) == before
    assert engine._submissions.empty()  # noqa: SLF001


@pytest.mark.anyio
async def test_root_first_late_cancel_does_not_claim_cancel_turn_attribution(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session root 已先取消 target 时，late CancelTurn 不得覆盖 first winner。"""
    client = _held_sim_client()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    try:
        target_id = await engine.submit(UserMessage(text="root-first"))
        await _wait_for_requests(client, 1)
        target_token = engine._pending[target_id].cancel  # noqa: SLF001
        coordinator.session_root_cancel.cancel()
        with anyio.fail_after(2):
            await target_token.wait_cancelled()
        monkeypatch.setattr(
            "taifeng.loop.submission.secrets.token_hex",
            lambda _: "rootlate",
        )
        resolution_entered = anyio.Event()
        registry = coordinator._target_cancellations  # noqa: SLF001
        original_start_resolution = registry._start_resolution  # noqa: SLF001

        def observe_start_resolution(target_submission_id: str) -> Any:
            """在 first-winner 判定完成后放行测试，消除 unregister 先跑竞态。"""
            resolution = original_start_resolution(target_submission_id)
            resolution_entered.set()
            return resolution

        monkeypatch.setattr(
            registry,
            "_start_resolution",
            observe_start_resolution,
        )
        cancel_task = asyncio.create_task(
            engine.submit(CancelTurn(submission_id=target_id))
        )
        with anyio.fail_after(2):
            await resolution_entered.wait()
        client.coordinator.signal("release-target")
        cancel_id = await cancel_task
        records = await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id=cancel_id,
        )
        applied = SubmissionAppliedV1.model_validate(
            next(
                envelope.payload
                for envelope in records
                if envelope.submission_id == cancel_id
                and envelope.record_type == "submission_applied"
            )
        )

        assert applied.result_status == "not_found"
        assert applied.terminal_record_ids == ()
        assert not any(
            envelope.record_type == "turn_cancelled"
            and envelope.submission_id == target_id
            for envelope in records
        )
        assert coordinator.health is AuditHealth.HEALTHY
    finally:
        client.coordinator.signal("release-target")
        await _stop_actor(actor)
