"""Journal-first UserMessage admission 与 actor 消费 token。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import anyio

from taifeng.conversation.journal.canonical import model_canonical_data
from taifeng.conversation.journal.errors import JournalIntegrityError
from taifeng.conversation.journal.framing import validate_envelope_chain
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
    record_id,
    validate_attachments,
)
from taifeng.conversation.models import ResponseItem
from taifeng.loop.submission import Submission, UserMessage

if TYPE_CHECKING:
    from taifeng.conversation.journal.projector import JournalConversationProjector
    from taifeng.loop.audit import JournalAppendReceipt, SessionAuditCoordinator
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
class AuditedUserMessageSubmission:
    """审计专用的完整 frozen submission 与首次 admission turn 事实。"""

    submission_id: str
    submitted_at: datetime
    accepted_turn_index: int
    text: str
    attachments: tuple[AttachmentV1, ...]

    def __post_init__(self) -> None:
        """拒绝无法形成稳定 Journal identity 的内部值。"""
        if not self.submission_id:
            raise ValueError("submission_id must be non-empty")
        if self.submitted_at.tzinfo is None or self.submitted_at.utcoffset() is None:
            raise ValueError("submitted_at must include timezone")
        if (
            isinstance(self.accepted_turn_index, bool)
            or self.accepted_turn_index < 0
        ):
            raise ValueError("accepted_turn_index must be non-negative")

    @property
    def id(self) -> str:
        """提供 stable submission identity。"""
        return self.submission_id


@dataclass(frozen=True, slots=True)
class _PreparedUserMessage:
    """首次 await 前冻结的 audited UserMessage 输入。"""

    submission_id: str
    submitted_at: datetime
    text: str
    attachments: tuple[AttachmentV1, ...]

    def accept(self, turn_index: int) -> AuditedUserMessageSubmission:
        """在 admission 顺序点绑定唯一 turn index。"""
        return AuditedUserMessageSubmission(
            submission_id=self.submission_id,
            submitted_at=self.submitted_at,
            accepted_turn_index=turn_index,
            text=self.text,
            attachments=self.attachments,
        )


@dataclass(frozen=True, slots=True)
class AcceptedUserMessage:
    """只携 durable receipt 的 actor queue token，不保留原始 UserMessage Op。"""

    submission_id: str
    accepted_work: AcceptedWork
    ack: JournalAck
    envelopes: tuple[JournalEnvelope, ...]
    op: None = None

    @property
    def id(self) -> str:
        """提供 queue routing 的 submission identity 兼容视图。"""
        return self.submission_id

    @property
    def accepted_record_ids(self) -> tuple[str, ...]:
        """返回 covering ack 的完整三记录 identity。"""
        return self.ack.record_ids

    @property
    def conversation_envelopes(self) -> tuple[JournalEnvelope, ...]:
        """返回 projector 唯一允许消费的 acknowledged conversation envelope。"""
        return (self.envelopes[1],) if len(self.envelopes) == 3 else ()

    def validated_application(
        self,
    ) -> tuple[ResponseItem, tuple[JournalEnvelope, ...]]:
        """纯校验完整 token lineage 后返回 hot item 与 projector 输入。"""
        ack, envelopes = self._defensive_receipt()
        if not self._receipt_shape_is_valid(ack, envelopes):
            raise _InvalidAcceptedUserMessageError
        accepted = SubmissionAcceptedV1.model_validate(envelopes[0].payload)
        conversation = ConversationItemV1.model_validate(envelopes[1].payload)
        applied = SubmissionAppliedV1.model_validate(envelopes[2].payload)
        item = deserialize_response_item(conversation)
        attachments = [
            model_canonical_data(attachment)
            for attachment in accepted.attachments or ()
        ]
        valid = (
            accepted.op_kind == "user_message"
            and accepted.source == "user"
            and conversation.source_record_id == envelopes[0].record_id
            and conversation.thread_id == envelopes[0].thread_id
            and applied.accepted_record_id == envelopes[0].record_id
            and applied.result_status == "applied"
            and applied.conversation_item_ids == (envelopes[1].record_id,)
            and applied.terminal_record_ids == ()
            and item.payload == {"text": accepted.text, "attachments": attachments}
        )
        if not valid:
            raise _InvalidAcceptedUserMessageError
        return item, (envelopes[1],)

    def _defensive_receipt(
        self,
    ) -> tuple[JournalAck, tuple[JournalEnvelope, ...]]:
        """重建 frozen receipt，阻断低层属性篡改污染 actor。"""
        if type(self.ack) is not JournalAck or any(
            type(envelope) is not JournalEnvelope for envelope in self.envelopes
        ):
            raise _InvalidAcceptedUserMessageError
        ack = JournalAck.model_validate(self.ack.model_dump(mode="python"))
        envelopes = tuple(
            JournalEnvelope.model_validate(envelope.model_dump(mode="python"))
            for envelope in self.envelopes
        )
        return ack, envelopes

    def _receipt_shape_is_valid(
        self,
        ack: JournalAck,
        envelopes: tuple[JournalEnvelope, ...],
    ) -> bool:
        """校验三记录顺序、covering ack、identity 与完整 record lineage。"""
        if len(envelopes) != 3:
            return False
        if not self._hash_chain_is_valid(ack, envelopes):
            return False
        accepted_id = envelopes[0].record_id
        expected_types = (
            "submission_accepted",
            "conversation_item",
            "submission_applied",
        )
        return (
            tuple(envelope.record_type for envelope in envelopes) == expected_types
            and tuple(envelope.record_id for envelope in envelopes) == ack.record_ids
            and tuple(envelope.seq for envelope in envelopes)
            == tuple(range(ack.first_seq, ack.last_seq + 1))
            and ack.tail_hash == envelopes[-1].record_hash
            and all(self._common_envelope_identity(envelope) for envelope in envelopes)
            and len({envelope.thread_id for envelope in envelopes}) == 1
            and envelopes[0].causation_id is None
            and envelopes[1].causation_id == accepted_id
            and envelopes[2].causation_id == accepted_id
        )

    @staticmethod
    def _hash_chain_is_valid(
        ack: JournalAck,
        envelopes: tuple[JournalEnvelope, ...],
    ) -> bool:
        """复用 Journal strict codec 重算三记录 payload/record/hash chain。"""
        try:
            validate_envelope_chain(
                envelopes,
                session_id=ack.session_id,
                first_seq=ack.first_seq,
                previous_hash=envelopes[0].previous_hash,
            )
        except JournalIntegrityError:
            return False
        return True

    def _common_envelope_identity(self, envelope: JournalEnvelope) -> bool:
        """校验 admission 三记录共有且不可省略的 stable identity 字段。"""
        return (
            envelope.record_id
            == record_id(self.submission_id, envelope.record_type, ordinal=0)
            and envelope.operation_id == self.submission_id
            and envelope.submission_id == self.submission_id
            and envelope.thread_id is not None
            and envelope.attempt_id is None
            and envelope.turn_id is None
            and envelope.parent_record_id is None
            and envelope.correlation_id is None
            and envelope.actor == ActorRef(kind="user", source="user")
            and envelope.session_id == self.ack.session_id
            and envelope.writer_epoch == self.ack.writer_epoch
        )


@dataclass(frozen=True, slots=True)
class ReplayedUserMessage:
    """historical durable receipt 的 no-op 结果，不能进入 actor queue。"""

    submission_id: str
    ack: JournalAck
    envelopes: tuple[JournalEnvelope, ...]
    op: None = None

    @property
    def id(self) -> str:
        """返回被确认已处理的 submission identity。"""
        return self.submission_id


class _InvalidAcceptedUserMessageError(Exception):
    """内部 accepted token 不能证明完整 admission batch。"""


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


def prepare_user_message(
    state: AuditedAdmissionState,
    submission: Submission,
    *,
    submitted_at: datetime | None = None,
) -> _PreparedUserMessage:
    """在 Engine 第一个 await 前复制并 canonicalize legacy Submission。"""
    if not isinstance(submission.op, UserMessage):
        raise TypeError("audited UserMessage admission requires UserMessage")
    return _PreparedUserMessage(
        submission_id=submission.id,
        submitted_at=submitted_at or datetime.now(UTC),
        text=submission.op.text,
        attachments=_validated_attachments(submission.op, state),
    )


def _submission_records(
    state: AuditedAdmissionState,
    submission: AuditedUserMessageSubmission,
) -> tuple[JournalRecord, ...]:
    """构造 acceptance、user conversation item 与 applied 的原子三记录。"""
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
            turn_index=submission.accepted_turn_index,
            text=submission.text,
            attachments=submission.attachments,
            source="user",
        ),
        submission_id=submission.id,
        thread_id=state.thread_id,
    )
    item = ResponseItem(
        kind="user_message",
        id=f"item_{submission.id}",
        thread_id=state.thread_id,
        payload={
            "text": submission.text,
            "attachments": [
                model_canonical_data(attachment)
                for attachment in submission.attachments
            ],
        },
        created_at=submission.submitted_at,
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
    submission: AuditedUserMessageSubmission,
) -> AcceptedUserMessage | ReplayedUserMessage:
    """在 coordinator admission lifecycle 内先 durable commit，再生成 queue token。"""
    receipt: JournalAppendReceipt | None = None
    records: tuple[JournalRecord, ...] | None = None

    async def durable_accept() -> None:
        nonlocal receipt, records
        records = _submission_records(state, submission)
        receipt = await state.coordinator.append_batch_receipt(records)

    work = await state.coordinator.admit_work(submission.id, durable_accept)
    try:
        assert receipt is not None and records is not None
        envelopes = await state.coordinator.load_acknowledged(receipt.ack, records)
        token = AcceptedUserMessage(
            submission_id=submission.id,
            accepted_work=work,
            ack=receipt.ack,
            envelopes=envelopes,
        )
        token.validated_application()
    except BaseException as error:  # noqa: BLE001  # cancellation/fatal 必须先退休 ownership
        with anyio.CancelScope(shield=True):
            await work.complete()
        if isinstance(
            error,
            (KeyboardInterrupt, SystemExit, anyio.get_cancelled_exc_class()),
        ):
            raise
        raise state.coordinator.freeze(error) from None
    if receipt.historical:
        await work.complete()
        return ReplayedUserMessage(
            submission_id=token.submission_id,
            ack=token.ack,
            envelopes=token.envelopes,
        )
    return token


__all__ = [
    "AcceptedUserMessage",
    "AuditedAdmissionState",
    "AuditedUserMessageSubmission",
    "ReplayedUserMessage",
    "admit_user_message",
    "prepare_user_message",
]
