"""SessionJournal 业务集成的版本化 record payload 与稳定身份。"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Sequence  # noqa: TC003  # 运行期签名需要
from typing import Annotated, Literal, Self

from pydantic import (
    Discriminator,
    Field,
    Tag,
    model_validator,
)

from taifeng.conversation.journal.attachment_records import FileAttachmentRecordV1
from taifeng.conversation.journal.canonical import (
    canonical_hash,
)
from taifeng.conversation.journal.item_records import (  # noqa: E402
    UnsupportedConversationItemError as UnsupportedConversationItemError,
)
from taifeng.conversation.journal.item_records import (
    _AssistantMessageItemPayload as _AssistantMessageItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _FunctionCallItemPayload as _FunctionCallItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _FunctionCallOutputItemPayload as _FunctionCallOutputItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _ProviderStateEnvelopeV1 as _ProviderStateEnvelopeV1,
)
from taifeng.conversation.journal.item_records import (
    _ReasoningItemPayload as _ReasoningItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _SkillOutcomeItemPayload as _SkillOutcomeItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _supported_item_kind as _supported_item_kind,
)
from taifeng.conversation.journal.item_records import (
    _UserMessageItemPayload as _UserMessageItemPayload,
)
from taifeng.conversation.journal.item_records import (
    _validated_item_metadata as _validated_item_metadata,
)
from taifeng.conversation.journal.item_records import (
    _validated_item_payload as _validated_item_payload,
)
from taifeng.conversation.journal.item_records import (
    conversation_item_record as conversation_item_record,
)
from taifeng.conversation.journal.item_records import (
    deserialize_response_item as deserialize_response_item,
)
from taifeng.conversation.journal.item_records import (
    serialize_response_item as serialize_response_item,
)
from taifeng.conversation.journal.models import (
    HashHex,
    JournalModel,
    NonEmptyStr,
)
from taifeng.conversation.journal.records_base import (
    _CONTEXT_OPERATIONS as _CONTEXT_OPERATIONS,
)
from taifeng.conversation.journal.records_base import (
    _MEDIA_COMPONENT as _MEDIA_COMPONENT,
)
from taifeng.conversation.journal.records_base import (
    _SUPPORTED_ITEM_KINDS as _SUPPORTED_ITEM_KINDS,
)

# 基类 / identity / factory 与对话项序列化已拆到兄弟模块（W7.1）；此处原名再导出，既有 import 路径不变
from taifeng.conversation.journal.records_base import (  # noqa: E402
    ApprovedSafeMessage as ApprovedSafeMessage,
)
from taifeng.conversation.journal.records_base import (
    CanonicalList as CanonicalList,
)
from taifeng.conversation.journal.records_base import (
    CanonicalMapping as CanonicalMapping,
)
from taifeng.conversation.journal.records_base import (
    ConversationItemV1 as ConversationItemV1,
)
from taifeng.conversation.journal.records_base import (
    JournalIdentities as JournalIdentities,
)
from taifeng.conversation.journal.records_base import (
    JournalRecordFactory as JournalRecordFactory,
)
from taifeng.conversation.journal.records_base import (
    LlmStatus as LlmStatus,
)
from taifeng.conversation.journal.records_base import (
    MediaType as MediaType,
)
from taifeng.conversation.journal.records_base import (
    NonNegativeInt as NonNegativeInt,
)
from taifeng.conversation.journal.records_base import (
    PayloadModel as PayloadModel,
)
from taifeng.conversation.journal.records_base import (
    PayloadModelV2 as PayloadModelV2,
)
from taifeng.conversation.journal.records_base import (
    SkillStatus as SkillStatus,
)
from taifeng.conversation.journal.records_base import (
    StableErrorV1 as StableErrorV1,
)
from taifeng.conversation.journal.records_base import (
    SupportedItemKind as SupportedItemKind,
)
from taifeng.conversation.journal.records_base import (
    ToolStatus as ToolStatus,
)
from taifeng.conversation.journal.records_base import (
    _canonical_list as _canonical_list,
)
from taifeng.conversation.journal.records_base import (
    _canonical_mapping as _canonical_mapping,
)
from taifeng.conversation.journal.records_base import (
    _decoded_base64_size as _decoded_base64_size,
)
from taifeng.conversation.journal.records_base import (
    _is_canonical_uint as _is_canonical_uint,
)
from taifeng.conversation.journal.records_base import (
    _operation_kind as _operation_kind,
)
from taifeng.conversation.journal.records_base import (
    record_id as record_id,
)
from taifeng.llm.errors import (
    AuthenticationError,
    CancelledError,
    ContentFilterError,
    ContextOverflowError,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    RequestTooLargeError,
    ServerError,
    TransientNetworkError,
)
from taifeng.tool.spec import ToolResult


class AttachmentV1(PayloadModel):
    """完整内联 base64 附件；V1 不接受 URI 或临时路径。"""

    kind: NonEmptyStr
    media_type: MediaType
    size: NonNegativeInt
    sha256: HashHex
    encoding: Literal["base64"] = "base64"
    content: NonEmptyStr
    detail: Literal["auto", "low", "high", "original"] = "auto"

    def decoded(self) -> bytes:
        """严格解码并校验声明 size 和小写 SHA-256。"""
        try:
            _decoded_base64_size(self.content)
            encoded = self.content.encode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("attachment content is not strict base64") from exc
        if base64.b64encode(decoded).decode("ascii") != self.content:
            raise ValueError("attachment content is not canonical base64")
        if len(decoded) != self.size:
            raise ValueError("attachment decoded size mismatch")
        if hashlib.sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("attachment SHA-256 mismatch")
        return decoded

def _attachment_shape(value: object) -> str:
    """按 ``kind`` 区分附件形状：``file`` 走文件 DTO，其余走 ``AttachmentV1``。"""
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    return "file" if kind == "file" else "inline"

AcceptedAttachment = Annotated[
    Annotated[AttachmentV1, Tag("inline")] | Annotated[FileAttachmentRecordV1, Tag("file")],
    Discriminator(_attachment_shape),
]
"""一条已接受的附件：图片（``AttachmentV1``）或文件（``FileAttachmentRecordV1``，ADR 0095）。"""

class SubmissionAcceptedV1(PayloadModel):
    """按 op_kind 严格区分 UserMessage/CancelTurn/Shutdown 的 durable acceptance。"""

    op_kind: Literal["user_message", "cancel_turn", "shutdown"]
    turn_index: NonNegativeInt | None = None
    text: str | None = None
    attachments: tuple[AcceptedAttachment, ...] | None = None
    source: NonEmptyStr | None = None
    target_submission_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_op_shape(self) -> Self:
        """要求当前 discriminator 形状的完整字段，拒绝交叉字段。"""
        values = {
            "text": self.text,
            "attachments": self.attachments,
            "source": self.source,
            "target_submission_id": self.target_submission_id,
        }
        if self.op_kind == "user_message":
            if any(values[name] is None for name in ("text", "attachments", "source")):
                raise ValueError("user_message requires text, attachments, and source")
            if self.target_submission_id is not None:
                raise ValueError("user_message rejects cancel_turn fields")
        elif self.op_kind == "cancel_turn":
            if self.target_submission_id is None:
                raise ValueError("cancel_turn requires target_submission_id")
            if any(values[name] is not None for name in ("text", "attachments", "source")):
                raise ValueError("cancel_turn rejects user_message fields")
            if self.turn_index is not None:
                raise ValueError("cancel_turn rejects turn_index")
        elif any(value is not None for value in values.values()) or self.turn_index is not None:
            raise ValueError("shutdown has no business payload")
        return self

class SubmissionAppliedV1(PayloadModel):
    """已接受 submission 的最终应用结果。"""

    accepted_record_id: NonEmptyStr
    result_status: NonEmptyStr
    conversation_item_ids: tuple[str, ...]
    terminal_record_ids: tuple[str, ...]

class SubmissionRejectedV1(PayloadModel):
    """Submission 在 effect 前被稳定拒绝的安全描述。"""

    op_kind: NonEmptyStr
    stable_error: StableErrorV1
    input_descriptor_hash: HashHex

class TurnStartedV1(PayloadModel):
    """Turn 执行入口快照。"""

    turn_index: NonNegativeInt
    entry_skill_id: NonEmptyStr
    skill_snapshot_version: NonEmptyStr
    model: NonEmptyStr
    budget_snapshot: CanonicalMapping

class TurnCompletedV1(PayloadModel):
    """Turn 成功终态。"""

    turn_index: NonNegativeInt
    end_reason: NonEmptyStr
    iterations: NonNegativeInt
    usage: CanonicalMapping
    final_item_ids: tuple[str, ...]

class TurnFailedV1(PayloadModel):
    """Turn 失败终态及 effect 已知状态。"""

    turn_index: NonNegativeInt
    stable_error: StableErrorV1
    effect_state: CanonicalMapping

class TurnCancelledV1(PayloadModel):
    """Turn 取消终态及 effect 已知状态。"""

    turn_index: NonNegativeInt
    cancellation_reason: NonEmptyStr
    effect_state: CanonicalMapping

class LlmRequestCommittedV1(PayloadModel):
    """LLM 真实 network attempt 发送前的 durable intent。"""

    turn_index: NonNegativeInt
    iteration: NonNegativeInt
    provider: NonEmptyStr
    model: NonEmptyStr
    api_request: CanonicalMapping
    effect_kind: NonEmptyStr
    idempotency_key: str | None = None
    reconciliation: NonEmptyStr

class RedactionEntryV1(JournalModel):
    """request 安全投影中一个被删除值的稳定地址。"""

    path: NonEmptyStr
    kind: Literal["image_base64", "file_base64", "provider_encrypted_content"]

class LlmRequestCommittedV2(PayloadModelV2):
    """不复制敏感正文的 LLM network attempt durable intent。"""

    turn_index: NonNegativeInt
    iteration: NonNegativeInt
    provider: NonEmptyStr
    model: NonEmptyStr
    api_request_safe: CanonicalMapping
    redactions: tuple[RedactionEntryV1, ...]
    canonical_attempt_sha256: HashHex
    effect_kind: NonEmptyStr
    idempotency_key: str | None = None
    reconciliation: NonEmptyStr

def parse_llm_request_committed(
    payload: object,
) -> LlmRequestCommittedV1 | LlmRequestCommittedV2:
    """按 payload_version 读取 request intent；新 writer 只产生 V2。"""
    if not isinstance(payload, dict):
        raise ValueError("llm request payload must be an object")
    version = payload.get("payload_version")
    if version == 1:
        return LlmRequestCommittedV1.model_validate(payload)
    if version == 2:
        return LlmRequestCommittedV2.model_validate(payload)
    raise ValueError(f"unsupported llm request payload version: {version!r}")

class LlmResponseCheckpointV1(PayloadModel):
    """Provider attempt 在 delta/retry 可见前的 durable checkpoint。"""

    request_record_id: NonEmptyStr
    retry_ordinal: NonNegativeInt
    status: LlmStatus
    normalized_items: CanonicalList
    usage: CanonicalMapping | None = None
    provider_request_id: str | None = None
    stable_error: StableErrorV1 | None = None

class LlmResponseCommittedV1(PayloadModel):
    """TurnRunner 观测到的最终 logical LLM response。"""

    request_record_id: NonEmptyStr
    checkpoint_record_id: NonEmptyStr
    status: LlmStatus
    normalized_items: CanonicalList
    usage: CanonicalMapping
    provider_request_id: str | None = None
    stable_error: StableErrorV1 | None = None

class ToolIntentCommittedV1(PayloadModel):
    """Tool runtime dispatch 之前已 durable 的意图。"""

    turn_index: NonNegativeInt
    iteration: NonNegativeInt
    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments_raw: str
    effective_arguments: CanonicalMapping
    parallel_safe: bool
    effect_kind: NonEmptyStr
    idempotency_key: str | None = None
    reconciliation: NonEmptyStr

class ToolOutcomeCommittedV1(PayloadModel):
    """Tool intent 的 durable 终态。"""

    intent_record_id: NonEmptyStr
    call_id: NonEmptyStr
    name: NonEmptyStr
    status: ToolStatus
    output: str
    data: CanonicalMapping
    duration_ms: Annotated[float, Field(ge=0)]
    stable_error: StableErrorV1 | None = None

class SkillSelectedV1(PayloadModel):
    """Skill dispatch 之前的完整 definition/body 快照。"""

    call_id: NonEmptyStr
    skill_id: NonEmptyStr
    version: NonEmptyStr
    definition_hash: HashHex
    body_hash: HashHex
    full_definition: CanonicalMapping
    arguments: CanonicalMapping
    selection_origin: NonEmptyStr
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

class SkillDispatchStartedV1(PayloadModel):
    """Skill child thread 启动的 lineage 快照。"""

    selected_record_id: NonEmptyStr
    call_id: NonEmptyStr
    parent_call_id: str | None = None
    child_thread_id: NonEmptyStr
    call_stack: tuple[str, ...]
    arguments: CanonicalMapping

class SkillDispatchFinishedV1(PayloadModel):
    """Skill dispatch 的 durable 终态，允许 quota rejection 无 started record。"""

    started_record_id: NonEmptyStr | None = None
    call_id: NonEmptyStr
    child_thread_id: NonEmptyStr | None = None
    status: SkillStatus
    end_reason: str | None = None
    final_text: str | None = None
    usage: CanonicalMapping | None = None
    stable_error: StableErrorV1 | None = None

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        """拒绝 rejection 伪 lineage 和非 rejection 缺 lineage。"""
        lineage = (self.started_record_id, self.child_thread_id)
        if self.status is SkillStatus.REJECTED and lineage != (None, None):
            raise ValueError("rejected skill dispatch forbids started/child lineage")
        if self.status is not SkillStatus.REJECTED and any(item is None for item in lineage):
            raise ValueError("non-rejected skill dispatch requires started/child lineage")
        return self

class ThreadCreatedV1(PayloadModel):
    """V1 child/root thread 创建描述（V0 初始化不使用此 DTO）。"""

    entry_skill_id: NonEmptyStr
    source: NonEmptyStr
    tags: tuple[str, ...]
    extra: CanonicalMapping
    parent_thread_id: str | None = None
    call_id: str | None = None

class ThreadBoundV1(PayloadModel):
    """Session 与 thread 的 V1 binding。"""

    session_id: NonEmptyStr
    thread_id: NonEmptyStr
    call_id: str | None = None

class ThreadTerminalV1(PayloadModel):
    """Thread 的 durable 终态。"""

    status: NonEmptyStr
    end_reason: NonEmptyStr
    stable_error: StableErrorV1 | None = None

class SessionEndedV1(PayloadModel):
    """Session 的唯一 durable 终态。"""

    status: NonEmptyStr
    reason: NonEmptyStr
    audit_complete: bool

# turn 内按序号编号的 operation：``{turn_id}:<kind>:<ordinal>``
# 上下文维护（ADR 0094）、hook 与权限裁决（ADR 0096）、挂起（ADR 0097）

_PUBLIC_LLM_ERRORS: tuple[type[LLMError], ...] = (
    RateLimitError, TransientNetworkError, ServerError, ContentFilterError, ContextOverflowError,
    AuthenticationError, InvalidRequestError, CancelledError, RequestTooLargeError,
)

def stable_error(
    error: BaseException | ToolResult,
    *,
    approved_message: ApprovedSafeMessage | None = None,
) -> StableErrorV1:
    """把已批准公开错误/ToolResult 或未知异常映射为安全 DTO。"""
    if isinstance(error, ToolResult):
        code = "tool_result_error" if error.is_error else "tool_result"
        class_name = "ToolResult"
        failure_class = "tool_error" if error.is_error else "none"
        retryable = False
        safe_message: str | None = approved_message.text if approved_message else None
    else:
        class_name = type(error).__name__
        if type(error) in _PUBLIC_LLM_ERRORS:
            code = error.kind  # type: ignore[attr-defined]
            failure_class = error.failure_class  # type: ignore[attr-defined]
            retryable = error.retryable  # type: ignore[attr-defined]
            safe_message = approved_message.text if approved_message else None
        else:
            code = "unknown_exception"
            failure_class = "unknown"
            retryable = False
            safe_message = None
    descriptor = {"code": code, "class_name": class_name,
                  "failure_class": failure_class, "retryable": retryable}
    descriptor_hash = canonical_hash(descriptor)
    return StableErrorV1(
        code=code,
        class_name=class_name,
        failure_class=failure_class,
        safe_message=safe_message,
        descriptor_hash=descriptor_hash,
        retryable=retryable,
    )

def validate_attachments(
    attachments: Sequence[AttachmentV1 | FileAttachmentRecordV1],
    *,
    max_item_bytes: int,
    max_total_bytes: int,
) -> tuple[bytes, ...]:
    """在 acceptance 前使用注入上限校验附件声明与完整正文。"""
    if max_item_bytes < 0 or max_total_bytes < 0:
        raise ValueError("attachment byte limits must be non-negative")
    decoded_total = 0
    for item in attachments:
        remaining = max_total_bytes - decoded_total
        encoded_budget = ((min(max_item_bytes, remaining) + 2) // 3) * 4
        if len(item.content) > encoded_budget:
            limit = "total" if remaining < max_item_bytes else "per-item"
            raise ValueError(f"attachment encoded {limit} byte limit exceeded")
        decoded_size = _decoded_base64_size(item.content)
        if decoded_size > max_item_bytes:
            raise ValueError("attachment encoded per-item byte limit exceeded")
        decoded_total += decoded_size
        if decoded_total > max_total_bytes:
            raise ValueError("attachment encoded total byte limit exceeded")
    return tuple(item.decoded() for item in attachments)
