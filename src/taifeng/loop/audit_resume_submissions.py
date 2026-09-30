"""审计 Session resume 时应用已准入、尚未应用的用户消息（ADR 0101）。

准入与应用是两个批次。进程死在两者之间时——消息排在运行中的 turn 后面，或者刚准入还没轮到
——Journal 里有 ``submission_accepted`` 而没有 ``submission_applied``。准入是 durable 承诺：
接管时把这些消息按准入顺序应用，对话项落在对话的末尾。

它们的 turn 不会被补跑：消息进入了对话，模型在下一个 turn 看到它。

本模块只做纯计算；记录的追加与全有或全无的语义由 ``audit_resume`` 负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.journal.records import SubmissionAcceptedV1
from taifeng.loop.audit_admission import accepted_user_item, application_records

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord


def unapplied_user_messages(
    envelopes: Sequence[JournalEnvelope], pending: Collection[str], thread_id: str,
) -> tuple[JournalEnvelope, ...]:
    """未结算 record 里该 thread 上已准入的用户消息，按准入顺序。

    Raises:
        pydantic.ValidationError: 准入记录形状违约（Journal 不可信）。
    """
    return tuple(
        envelope for envelope in envelopes
        if envelope.record_type == "submission_accepted"
        and envelope.record_id in pending
        and envelope.thread_id == thread_id
        and envelope.submission_id is not None
        and SubmissionAcceptedV1.model_validate(envelope.payload).op_kind == "user_message"
    )


def application_recovery_records(
    accepted: JournalEnvelope, *, session_id: str, recovery_operation_id: str,
) -> tuple[JournalRecord, JournalRecord]:
    """一条未应用消息的应用批次；对话项与运行时应用得到的逐字相同。"""
    assert accepted.thread_id is not None
    assert accepted.submission_id is not None
    item = accepted_user_item(
        submission_id=accepted.submission_id,
        thread_id=accepted.thread_id,
        accepted=SubmissionAcceptedV1.model_validate(accepted.payload),
        submitted_at=accepted.occurred_at or accepted.recorded_at,
    )
    return application_records(
        session_id=session_id,
        thread_id=accepted.thread_id,
        submission_id=accepted.submission_id,
        accepted_record_id=accepted.record_id,
        item=item,
        correlation_id=recovery_operation_id,
    )


__all__ = ["application_recovery_records", "unapplied_user_messages"]
