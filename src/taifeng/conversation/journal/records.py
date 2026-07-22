"""SessionJournal 业务集成的版本化 record payload 与稳定身份。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003  # Pydantic 运行期需要
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from taifeng.conversation.journal.canonical import (
    canonical_hash,
    model_canonical_data,
    validate_json_value,
)
from taifeng.conversation.journal.models import (
    ActorRef,
    HashHex,
    JournalModel,
    JournalRecord,
    JsonValue,
    NonEmptyStr,
)
from taifeng.conversation.models import ResponseItem
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

type SupportedItemKind = Literal[
    "user_message",
    "assistant_message",
    "function_call",
    "function_call_output",
    "reasoning",
    "skill_outcome",
]

_SUPPORTED_ITEM_KINDS: frozenset[str] = frozenset(
    {
        "user_message",
        "assistant_message",
        "function_call",
        "function_call_output",
        "reasoning",
        "skill_outcome",
    }
)


def _canonical_mapping(value: object) -> dict[str, JsonValue]:
    """校验自由 mapping 是纯 JsonValue，不进行隐式字符串化。"""
    normalized = validate_json_value(value)
    if not isinstance(normalized, dict):
        raise ValueError("value must be a canonical JSON mapping")
    return normalized


def _canonical_list(value: object) -> list[JsonValue]:
    """校验自由 sequence 是纯 JsonValue list。"""
    normalized = validate_json_value(value)
    if not isinstance(normalized, list):
        raise ValueError("value must be a canonical JSON list")
    return normalized


CanonicalMapping = Annotated[dict[str, JsonValue], BeforeValidator(_canonical_mapping)]
CanonicalList = Annotated[list[JsonValue], BeforeValidator(_canonical_list)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
MediaType = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"),
]

_BASE64_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
)


def _decoded_base64_size(content: str) -> int:
    """不分配 decoded bytes 地校验 base64 形状并计算精确长度。"""
    if not _BASE64_PATTERN.fullmatch(content):
        raise ValueError("attachment content is not strict base64")
    padding = len(content) - len(content.rstrip("="))
    return len(content) // 4 * 3 - padding


class PayloadModel(JournalModel):
    """V1 业务 payload 的统一冻结、禁 extra 基类。"""

    payload_version: Literal[1] = 1


class LlmStatus(StrEnum):
    """LLM attempt/logical response 的稳定终态。"""

    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ToolStatus(StrEnum):
    """Tool intent 的稳定终态。"""

    SUCCESS = "success"
    ERROR = "error"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class SkillStatus(StrEnum):
    """Skill dispatch 的稳定终态。"""

    SUCCESS = "success"
    ERROR = "error"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# 兼容更明确的领域命名，二者仍是同一枚举类型。
LlmResponseStatus = LlmStatus
ToolOutcomeStatus = ToolStatus
SkillDispatchStatus = SkillStatus


class StableErrorV1(PayloadModel):
    """不携带 traceback/repr/地址的稳定错误描述。"""

    code: NonEmptyStr
    class_name: NonEmptyStr
    failure_class: NonEmptyStr
    safe_message: str | None = None
    descriptor_hash: HashHex | None = None
    retryable: bool = False


class AttachmentV1(PayloadModel):
    """完整内联 base64 附件；V1 不接受 URI 或临时路径。"""

    kind: NonEmptyStr
    media_type: MediaType
    size: NonNegativeInt
    sha256: HashHex
    encoding: Literal["base64"] = "base64"
    content: NonEmptyStr

    def decoded(self) -> bytes:
        """严格解码并校验声明 size 和小写 SHA-256。"""
        try:
            _decoded_base64_size(self.content)
            encoded = self.content.encode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("attachment content is not strict base64") from exc
        if len(decoded) != self.size:
            raise ValueError("attachment decoded size mismatch")
        if hashlib.sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("attachment SHA-256 mismatch")
        return decoded


class SubmissionAcceptedV1(PayloadModel):
    """按 op_kind 严格区分 UserMessage/CancelTurn/Shutdown 的 durable acceptance。"""

    op_kind: Literal["user_message", "cancel_turn", "shutdown"]
    turn_index: NonNegativeInt | None = None
    text: str | None = None
    attachments: tuple[AttachmentV1, ...] | None = None
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
        elif any(value is not None for value in values.values()):
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
    duration_ms: NonNegativeFloat
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

    started_record_id: str | None = None
    call_id: NonEmptyStr
    child_thread_id: str | None = None
    status: SkillStatus
    end_reason: str | None = None
    final_text: str | None = None
    usage: CanonicalMapping | None = None
    stable_error: StableErrorV1 | None = None


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


class ConversationItemV1(PayloadModel):
    """ResponseItem 的显式、版本化 durable wire contract。"""

    item_version: Literal[1] = 1
    item_kind: SupportedItemKind
    thread_id: NonEmptyStr
    item_id: NonEmptyStr
    payload: CanonicalMapping
    created_at: datetime
    metadata: CanonicalMapping
    source_record_id: NonEmptyStr

    @field_validator("created_at")
    @classmethod
    def _require_aware_created_at(cls, value: datetime) -> datetime:
        """canonical wire 时间必须携时区。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include timezone")
        return value


class _UserMessageItemPayload(JournalModel):
    """user_message 的稳定 payload 形状。"""

    text: str
    attachments: CanonicalList


class _AssistantMessageItemPayload(JournalModel):
    """assistant_message 的稳定 payload 形状。"""

    text: str
    model: NonEmptyStr


class _FunctionCallItemPayload(JournalModel):
    """function_call 的稳定 payload 形状。"""

    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments: str


class _FunctionCallOutputItemPayload(JournalModel):
    """function_call_output 的稳定 payload 形状。"""

    call_id: NonEmptyStr
    output: str
    is_error: bool


class _ReasoningItemPayload(JournalModel):
    """reasoning 的稳定 payload 形状。"""

    text: str
    summary: str


class _SkillOutcomeItemPayload(JournalModel):
    """skill_outcome 当前完整形状，兼容早期最小记账项。"""

    skill_id: NonEmptyStr
    outcome: Literal["success", "failure", "abandoned"]
    call_id: str | None = None
    parent_call_id: str | None = None
    depth: NonNegativeInt | None = None
    source: str | None = None
    trust_tier: str | None = None
    selection_origin: str | None = None
    selection_confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    outcome_signal_source: str | None = None
    end_reason: str | None = None
    error_detail: str | None = None
    cost_tokens: NonNegativeInt | None = None
    cost_duration_ms: NonNegativeInt | None = None
    cost_iterations: NonNegativeInt | None = None
    ts_unix: NonNegativeInt | None = None


@dataclass(frozen=True, slots=True)
class JournalIdentities:
    """一个 submission 的稳定 operation identity 派生器。"""

    session_id: str
    thread_id: str
    submission_id: str

    def __post_init__(self) -> None:
        """拒绝会产生模糊 identity 的空组件。"""
        if not self.session_id or not self.thread_id or not self.submission_id:
            raise ValueError("journal identity components must be non-empty")

    def turn(self, index: int) -> str:
        """构造 root/child turn identity。"""
        if index < 0:
            raise ValueError("turn index must be non-negative")
        return f"{self.thread_id}:{self.submission_id}:turn:{index}"

    def llm(self, turn_id: str, iteration: int) -> str:
        """构造 logical LLM call identity。"""
        if iteration < 0:
            raise ValueError("LLM iteration must be non-negative")
        return f"{turn_id}:llm:{iteration}"

    def attempt(self, llm_operation_id: str, retry_ordinal: int) -> str:
        """构造真实 network attempt identity。"""
        if retry_ordinal < 0:
            raise ValueError("retry ordinal must be non-negative")
        return f"{llm_operation_id}:attempt:{retry_ordinal}"

    def tool(self, turn_id: str, call_id: str) -> str:
        """构造 Tool call identity。"""
        return f"{turn_id}:tool:{call_id}"

    def skill(self, tool_operation_id: str, target_skill_id: str) -> str:
        """构造 Skill dispatch identity。"""
        return f"{tool_operation_id}:skill:{target_skill_id}"


def record_id(
    operation_id: str,
    record_type: str,
    attempt_id: str | None = None,
    ordinal: int = 0,
) -> str:
    """构造不受 payload 内容影响的确定性 record id。"""
    if ordinal < 0:
        raise ValueError("record ordinal must be non-negative")
    attempt = attempt_id if attempt_id is not None else "none"
    return f"{operation_id}:{record_type}:{attempt}:{ordinal}"


@dataclass(frozen=True, slots=True)
class JournalRecordFactory:
    """固定 Session/actor 并显式填写已有 JournalRecord lineage 的 factory。"""

    session_id: str
    actor: ActorRef
    identities: JournalIdentities

    def __post_init__(self) -> None:
        """防止 factory 把其他 Session 的 identities 混入 record。"""
        if self.session_id != self.identities.session_id:
            raise ValueError("factory session_id must match identities")

    def build(
        self,
        *,
        operation_id: str,
        record_type: str,
        payload: PayloadModel,
        attempt_id: str | None = None,
        ordinal: int = 0,
        occurred_at: datetime | None = None,
        submission_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        parent_record_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> JournalRecord:
        """把 V1 payload canonicalize，同时保留调用方提供的全部内容。"""
        return JournalRecord(
            schema_version=1,
            session_id=self.session_id,
            record_id=record_id(operation_id, record_type, attempt_id, ordinal),
            record_type=record_type,
            actor=self.actor,
            payload=model_canonical_data(payload),
            operation_id=operation_id,
            attempt_id=attempt_id,
            occurred_at=occurred_at,
            submission_id=submission_id,
            thread_id=thread_id,
            turn_id=turn_id,
            parent_record_id=parent_record_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )


class UnsupportedConversationItemError(ValueError):
    """ResponseItem kind 未在当前 audit wire contract 白名单内。"""


def _supported_item_kind(kind: str) -> SupportedItemKind:
    """在任何 canonical payload/record 构造前拒绝未知 kind。"""
    if kind not in _SUPPORTED_ITEM_KINDS:
        raise UnsupportedConversationItemError(
            f"unsupported conversation item kind: {kind}"
        )
    return kind  # type: ignore[return-value]


def _validated_item_payload(
    kind: SupportedItemKind, payload: object
) -> dict[str, JsonValue]:
    """按 kind 的显式 required/extra 形状校验 payload。"""
    model: JournalModel
    if kind == "user_message":
        model = _UserMessageItemPayload.model_validate(payload)
    elif kind == "assistant_message":
        model = _AssistantMessageItemPayload.model_validate(payload)
    elif kind == "function_call":
        model = _FunctionCallItemPayload.model_validate(payload)
    elif kind == "function_call_output":
        model = _FunctionCallOutputItemPayload.model_validate(payload)
    elif kind == "reasoning":
        model = _ReasoningItemPayload.model_validate(payload)
    else:
        model = _SkillOutcomeItemPayload.model_validate(payload)
    return _canonical_mapping(model.model_dump(mode="python", exclude_unset=True))


def serialize_response_item(
    item: ResponseItem, *, source_record_id: str
) -> ConversationItemV1:
    """显式读取六个稳定 ResponseItem 字段，不使用通用 model_dump。"""
    kind = _supported_item_kind(str(item.kind))
    payload = _validated_item_payload(kind, item.payload)
    metadata = _canonical_mapping(item.metadata)
    return ConversationItemV1(
        item_kind=kind,
        thread_id=item.thread_id,
        item_id=item.id,
        payload=payload,
        created_at=item.created_at,
        metadata=metadata,
        source_record_id=source_record_id,
    )


def deserialize_response_item(payload: ConversationItemV1) -> ResponseItem:
    """从显式 V1 wire 字段恢复 ResponseItem，不使用通用 model_validate dump。"""
    kind = _supported_item_kind(str(payload.item_kind))
    item_payload = _validated_item_payload(kind, payload.payload)
    return ResponseItem(
        kind=kind,
        id=payload.item_id,
        thread_id=payload.thread_id,
        payload=item_payload,
        created_at=payload.created_at,
        metadata=_canonical_mapping(payload.metadata),
    )


def conversation_item_record(
    factory: JournalRecordFactory,
    *,
    operation_id: str,
    item: ResponseItem,
    source_record_id: str,
    ordinal: int,
    attempt_id: str | None = None,
    submission_id: str | None = None,
    turn_id: str | None = None,
) -> JournalRecord:
    """构造与来源 record/identity 链接的 conversation_item record。"""
    payload = serialize_response_item(item, source_record_id=source_record_id)
    return factory.build(
        operation_id=operation_id,
        record_type="conversation_item",
        payload=payload,
        attempt_id=attempt_id,
        ordinal=ordinal,
        submission_id=submission_id,
        thread_id=item.thread_id,
        turn_id=turn_id,
        causation_id=source_record_id,
    )


_PUBLIC_LLM_ERRORS: tuple[type[LLMError], ...] = (
    RateLimitError,
    TransientNetworkError,
    ServerError,
    ContentFilterError,
    ContextOverflowError,
    AuthenticationError,
    InvalidRequestError,
    CancelledError,
    RequestTooLargeError,
)


def stable_error(error: BaseException | ToolResult) -> StableErrorV1:
    """把已批准公开错误/ToolResult 或未知异常映射为安全 DTO。"""
    if isinstance(error, ToolResult):
        code = "tool_result_error" if error.is_error else "tool_result"
        class_name = "ToolResult"
        failure_class = "tool_error" if error.is_error else "none"
        retryable = False
        safe_message: str | None = error.output
    else:
        class_name = type(error).__name__
        if type(error) in _PUBLIC_LLM_ERRORS:
            code = error.kind  # type: ignore[attr-defined]
            failure_class = error.failure_class  # type: ignore[attr-defined]
            retryable = error.retryable  # type: ignore[attr-defined]
            safe_message = str(error)
        else:
            code = "unknown_exception"
            failure_class = "unknown"
            retryable = False
            safe_message = None
    descriptor_hash = canonical_hash(
        {
            "code": code,
            "class_name": class_name,
            "failure_class": failure_class,
            "retryable": retryable,
        }
    )
    return StableErrorV1(
        code=code,
        class_name=class_name,
        failure_class=failure_class,
        safe_message=safe_message,
        descriptor_hash=descriptor_hash,
        retryable=retryable,
    )


def validate_attachments(
    attachments: tuple[AttachmentV1, ...] | list[AttachmentV1],
    *,
    max_item_bytes: int,
    max_total_bytes: int,
) -> tuple[bytes, ...]:
    """在 acceptance 前使用注入上限校验附件声明与完整正文。"""
    if max_item_bytes < 0 or max_total_bytes < 0:
        raise ValueError("attachment byte limits must be non-negative")
    decoded_sizes = tuple(_decoded_base64_size(item.content) for item in attachments)
    if any(size > max_item_bytes for size in decoded_sizes):
        raise ValueError("attachment encoded per-item byte limit exceeded")
    if sum(decoded_sizes) > max_total_bytes:
        raise ValueError("attachment encoded total byte limit exceeded")
    if any(item.size > max_item_bytes for item in attachments):
        raise ValueError("attachment per-item byte limit exceeded")
    if sum(item.size for item in attachments) > max_total_bytes:
        raise ValueError("attachment total byte limit exceeded")
    return tuple(item.decoded() for item in attachments)


__all__ = [
    "AttachmentV1",
    "ConversationItemV1",
    "JournalIdentities",
    "JournalRecordFactory",
    "LlmRequestCommittedV1",
    "LlmResponseCheckpointV1",
    "LlmResponseCommittedV1",
    "LlmResponseStatus",
    "LlmStatus",
    "PayloadModel",
    "SessionEndedV1",
    "SkillDispatchFinishedV1",
    "SkillDispatchStartedV1",
    "SkillDispatchStatus",
    "SkillSelectedV1",
    "SkillStatus",
    "StableErrorV1",
    "SubmissionAcceptedV1",
    "SubmissionAppliedV1",
    "SubmissionRejectedV1",
    "ThreadBoundV1",
    "ThreadCreatedV1",
    "ThreadTerminalV1",
    "ToolIntentCommittedV1",
    "ToolOutcomeCommittedV1",
    "ToolOutcomeStatus",
    "ToolStatus",
    "TurnCancelledV1",
    "TurnCompletedV1",
    "TurnFailedV1",
    "TurnStartedV1",
    "UnsupportedConversationItemError",
    "conversation_item_record",
    "deserialize_response_item",
    "record_id",
    "serialize_response_item",
    "stable_error",
    "validate_attachments",
]
