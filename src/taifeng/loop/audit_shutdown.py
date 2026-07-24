"""Journal-first Shutdown admission 与 EnginePool ownership bridge。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.journal.models import ActorRef, JournalRecord
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    SubmissionAcceptedV1,
)
from taifeng.loop.audit_support import _await_owned
from taifeng.loop.submission import Shutdown, Submission

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from taifeng.loop.audit_bootstrap import AuditedSessionState


def _accepted_record(
    state: AuditedSessionState,
    submission: Submission,
) -> JournalRecord:
    """构造无业务 payload 的唯一 Shutdown acceptance。"""
    if not isinstance(submission.op, Shutdown):
        raise TypeError("audited Shutdown admission requires Shutdown")
    factory = JournalRecordFactory(
        session_id=state.coordinator.session_id,
        actor=ActorRef(kind="user", source="user"),
        identities=JournalIdentities(
            session_id=state.coordinator.session_id,
            thread_id=state.thread_id,
            submission_id=submission.id,
        ),
    )
    return factory.build(
        operation_id=submission.id,
        record_type="submission_accepted",
        payload=SubmissionAcceptedV1(op_kind="shutdown"),
        submission_id=submission.id,
        thread_id=state.thread_id,
    )


async def _submit_shutdown(
    state: AuditedSessionState,
    submission: Submission,
    admission_lock: asyncio.Lock,
    finish_owner: Callable[[], Awaitable[None]],
) -> str:
    """先在 admission 临界区 durable accept，再交给 pool-owned finish。"""
    record = _accepted_record(state, submission)
    async with admission_lock:
        claim = await state.coordinator.admit_shutdown(record)
    if claim.owner:
        state.coordinator.cancel_session_root()
        if claim.fatal is None:
            await finish_owner()
        else:
            try:
                await finish_owner()
            except BaseException:  # noqa: BLE001
                # acceptance fatal 已是唯一首因：owner 的任何失败只能唤醒
                # finish handoff，不能覆盖首因或改变普通非 fatal 路径。
                state.coordinator.fail_shutdown_finish_handoff()
            raise claim.fatal
    else:
        result = await state.coordinator.wait_shutdown_finish(submission.id)
        if not result.audit_complete or not result.lease_released:
            from taifeng.loop.audit_bootstrap import AuditSessionReleaseError

            raise AuditSessionReleaseError(
                state.coordinator.session_id,
                finish_result=result,
            )
    return submission.id


async def submit_audited_shutdown(
    state: AuditedSessionState,
    submission: Submission,
    admission_lock: asyncio.Lock,
    finish_owner: Callable[[], Awaitable[None]] | None,
) -> str:
    """owned operation 不让 raw caller cancellation 截断 finish/close。"""
    if finish_owner is None:
        raise RuntimeError("audited Shutdown owner is not configured")
    result, cancellation = await _await_owned(
        _submit_shutdown(state, submission, admission_lock, finish_owner),
        name=f"audit-shutdown:{state.coordinator.session_id}:{submission.id}",
    )
    if cancellation is not None:
        raise cancellation
    return result


def shutdown_submission(state: AuditedSessionState) -> Submission:
    """复用 durable acceptance id 构造 pool-owned shutdown submission。"""
    submission_id = state.coordinator.shutdown_submission_id
    return (
        Submission(id=submission_id, op=Shutdown())
        if submission_id is not None
        else Submission(op=Shutdown())
    )


__all__ = ["shutdown_submission", "submit_audited_shutdown"]
