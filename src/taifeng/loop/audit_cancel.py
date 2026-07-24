"""Journal-first CancelTurn admission 与 target 终态收敛。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.conversation.journal.models import ActorRef, JournalRecord
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    SubmissionAcceptedV1,
    SubmissionAppliedV1,
    TurnCancelledV1,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.audit_targets import TargetCancelResult
    from taifeng.loop.cancellation import CancellationToken


@dataclass(frozen=True, slots=True)
class AuditedCancelTurnSubmission:
    """审计专用的稳定 CancelTurn submission。"""

    submission_id: str
    target_submission_id: str

    def __post_init__(self) -> None:
        """拒绝无法形成 canonical operation identity 的输入。"""
        if not self.submission_id or not self.target_submission_id:
            raise ValueError("cancel and target submission ids must be non-empty")


def _factory(
    state: AuditedSessionState,
    submission_id: str,
    *,
    actor: ActorRef,
) -> JournalRecordFactory:
    """构造绑定当前 Session/thread/submission 的 record factory。"""
    return JournalRecordFactory(
        session_id=state.coordinator.session_id,
        actor=actor,
        identities=JournalIdentities(
            session_id=state.coordinator.session_id,
            thread_id=state.thread_id,
            submission_id=submission_id,
        ),
    )


def _accepted_record(
    state: AuditedSessionState,
    submission: AuditedCancelTurnSubmission,
) -> JournalRecord:
    """构造 CancelTurn 的 durable acceptance。"""
    return _factory(
        state,
        submission.submission_id,
        actor=ActorRef(kind="user", source="user"),
    ).build(
        operation_id=submission.submission_id,
        record_type="submission_accepted",
        payload=SubmissionAcceptedV1(
            op_kind="cancel_turn",
            target_submission_id=submission.target_submission_id,
        ),
        submission_id=submission.submission_id,
        thread_id=state.thread_id,
    )


def _applied_record(
    state: AuditedSessionState,
    submission: AuditedCancelTurnSubmission,
    result: TargetCancelResult,
    *,
    accepted_record_id: str,
) -> JournalRecord:
    """构造引用 target terminal ids 的 durable application。"""
    return _factory(
        state,
        submission.submission_id,
        actor=ActorRef(kind="system", source="engine"),
    ).build(
        operation_id=submission.submission_id,
        record_type="submission_applied",
        payload=SubmissionAppliedV1(
            accepted_record_id=accepted_record_id,
            result_status=result.result_status,
            conversation_item_ids=(),
            terminal_record_ids=result.terminal_record_ids,
        ),
        submission_id=submission.submission_id,
        thread_id=state.thread_id,
        causation_id=accepted_record_id,
    )


async def _apply_cancel_turn(
    state: AuditedSessionState,
    submission: AuditedCancelTurnSubmission,
) -> TargetCancelResult:
    """accepted ack 后触发 target，terminal 收敛后写 applied。"""
    accepted = _accepted_record(state, submission)

    async def durable_accept() -> None:
        """写入 durable acceptance。"""
        await state.coordinator.append(accepted)

    work = await state.coordinator.admit_work(
        submission.submission_id,
        durable_accept,
    )
    try:
        result = await state.coordinator.resolve_target_cancel(
            cancel_submission_id=submission.submission_id,
            target_submission_id=submission.target_submission_id,
        )
        applied = _applied_record(
            state,
            submission,
            result,
            accepted_record_id=accepted.record_id,
        )
        await state.coordinator.append(applied)
        return result
    finally:
        await work.complete()


async def _await_owned[T](operation: Coroutine[object, object, T]) -> T:
    """独立 task 完成 CancelTurn 收敛；延迟重抛 caller cancellation。"""
    task: asyncio.Task[T] = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def apply_cancel_turn(
    state: AuditedSessionState,
    submission: AuditedCancelTurnSubmission,
) -> TargetCancelResult:
    """以 cancellation-independent ownership 执行 healthy CancelTurn。"""
    return await _await_owned(_apply_cancel_turn(state, submission))


async def finalize_cancelled_target(
    state: AuditedSessionState,
    *,
    submission_id: str,
    turn_index: int,
    target_token: CancellationToken,
) -> str:
    """durable 写 turn_cancelled 后登记 target terminal identity。"""
    factory = _factory(
        state,
        submission_id,
        actor=ActorRef(kind="system", source="engine"),
    )
    turn_id = factory.identities.turn(turn_index)
    record = factory.build(
        operation_id=turn_id,
        record_type="turn_cancelled",
        payload=TurnCancelledV1(
            turn_index=turn_index,
            cancellation_reason="cancel_turn",
            effect_state={"known_status": "cancelled"},
        ),
        submission_id=submission_id,
        thread_id=state.thread_id,
        turn_id=turn_id,
    )

    async def commit_and_register() -> str:
        """保证 ack 先于 registry terminal 可见性。"""
        await state.coordinator.append(record)
        registered = state.coordinator.register_target_terminal(
            submission_id,
            target_token,
            terminal_record_ids=(record.record_id,),
        )
        if not registered:
            raise state.coordinator.freeze(
                RuntimeError("target terminal ownership mismatch")
            )
        return record.record_id

    return await _await_owned(commit_and_register())


__all__ = [
    "AuditedCancelTurnSubmission",
    "apply_cancel_turn",
    "finalize_cancelled_target",
]
