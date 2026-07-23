"""Journal-first UserMessage admission 与 actor 消费 token。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from taifeng.conversation.journal.canonical import model_canonical_data
from taifeng.conversation.journal.models import (
    ActorRef,
    JournalAck,
    JournalEnvelope,
    JournalRecord,
)
from taifeng.conversation.journal.records import (
    AttachmentV1,
    ConversationItemV1,
    JournalIdentities,
    JournalRecordFactory,
    SubmissionAcceptedV1,
    SubmissionAppliedV1,
    conversation_item_record,
    deserialize_response_item,
    validate_attachments,
)
from taifeng.conversation.models import ResponseItem, user_message
from taifeng.loop.submission import Submission, UserMessage

if TYPE_CHECKING:
    from taifeng.conversation.journal.projector import JournalConversationProjector
    from taifeng.loop.audit import SessionAuditCoordinator
    from taifeng.loop.audit_lifecycle import AcceptedWork


class AuditedAdmissionState(Protocol):
    """AgentEngine admission 依赖的最小 frozen ownership view。"""

    @property
    def thread_id(self) -> str:
        """返回 root thread id。"""

    @property
    def coordinator(self) -> SessionAuditCoordinator:
        """返回单 Session coordinator。"""

    @property
    def projector(self) -> JournalConversationProjector:
        """返回 ack-only projector。"""

    @property
    def max_attachment_bytes(self) -> int:
        """返回单附件上限。"""

    @property
    def max_total_attachment_bytes(self) -> int:
        """返回 submission 附件总上限。"""


@dataclass(frozen=True, slots=True)
class AcceptedUserMessage:
    """只携 durable receipt 的 actor queue token，不保留原始 UserMessage Op。"""

    submission_id: str
    accepted_work: AcceptedWork
    ack: JournalAck
    accepted_record_ids: tuple[str, ...]
    conversation_envelopes: tuple[JournalEnvelope, ...]
    op: None = None

    @property
    def id(self) -> str:
        """提供 queue routing 的 submission identity 兼容视图。"""
        return self.submission_id

    def response_item(self) -> ResponseItem:
        """只从 ack 覆盖的 conversation envelope 恢复 hot-history item。"""
        if len(self.conversation_envelopes) != 1:
            raise ValueError("accepted UserMessage requires one conversation envelope")
        envelope = self.conversation_envelopes[0]
        if envelope.record_id not in self.ack.record_ids:
            raise ValueError("conversation envelope is not covered by JournalAck")
        return deserialize_response_item(
            ConversationItemV1.model_validate(envelope.payload)
        )


def _validated_attachments(
    op: UserMessage,
    state: AuditedAdmissionState,
) -> tuple[AttachmentV1, ...]:
    """把自由 attachment mapping 收敛为 canonical V1 DTO 并校验内容上限。"""
    attachments = tuple(
        AttachmentV1.model_validate(attachment)
        for attachment in op.attachments
    )
    validate_attachments(
        attachments,
        max_item_bytes=state.max_attachment_bytes,
        max_total_bytes=state.max_total_attachment_bytes,
    )
    return attachments


def _submission_records(
    state: AuditedAdmissionState,
    submission: Submission,
    *,
    turn_index: int,
) -> tuple[JournalRecord, ...]:
    """构造 acceptance、user conversation item 与 applied 的原子三记录。"""
    if not isinstance(submission.op, UserMessage):
        raise TypeError("audited UserMessage admission requires UserMessage")
    attachments = _validated_attachments(submission.op, state)
    identities = JournalIdentities(
        session_id=state.coordinator.session_id,
        thread_id=state.thread_id,
        submission_id=submission.id,
    )
    factory = JournalRecordFactory(
        session_id=state.coordinator.session_id,
        actor=ActorRef(kind="user", source="user"),
        identities=identities,
    )
    accepted = factory.build(
        operation_id=submission.id,
        record_type="submission_accepted",
        payload=SubmissionAcceptedV1(
            op_kind="user_message",
            turn_index=turn_index,
            text=submission.op.text,
            attachments=attachments,
            source="user",
        ),
        submission_id=submission.id,
        thread_id=state.thread_id,
    )
    item = user_message(
        submission.op.text,
        thread_id=state.thread_id,
        attachments=[
            model_canonical_data(attachment)
            for attachment in attachments
        ],
    )
    conversation = conversation_item_record(
        factory,
        operation_id=submission.id,
        item=item,
        source_record_id=accepted.record_id,
        ordinal=0,
        submission_id=submission.id,
    )
    applied = factory.build(
        operation_id=submission.id,
        record_type="submission_applied",
        payload=SubmissionAppliedV1(
            accepted_record_id=accepted.record_id,
            result_status="applied",
            conversation_item_ids=(conversation.record_id,),
            terminal_record_ids=(),
        ),
        submission_id=submission.id,
        thread_id=state.thread_id,
        causation_id=accepted.record_id,
    )
    return accepted, conversation, applied


async def admit_user_message(
    state: AuditedAdmissionState,
    submission: Submission,
    *,
    turn_index: int,
) -> AcceptedUserMessage:
    """在 coordinator admission lifecycle 内先 durable commit，再生成 queue token。"""
    ack: JournalAck | None = None

    async def durable_accept() -> None:
        nonlocal ack
        records = _submission_records(state, submission, turn_index=turn_index)
        ack = await state.coordinator.append_batch(records)

    work = await state.coordinator.admit_work(submission.id, durable_accept)
    assert ack is not None
    envelopes = await state.coordinator.load_acknowledged(ack)
    conversation_envelopes = tuple(
        envelope
        for envelope in envelopes
        if envelope.record_type == "conversation_item"
    )
    return AcceptedUserMessage(
        submission_id=submission.id,
        accepted_work=work,
        ack=ack,
        accepted_record_ids=ack.record_ids,
        conversation_envelopes=conversation_envelopes,
    )


__all__ = ["AcceptedUserMessage", "AuditedAdmissionState", "admit_user_message"]
