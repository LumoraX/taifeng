"""SessionJournal 业务集成的版本化 record payload 与稳定身份。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003  # Pydantic 运行期需要
from enum import StrEnum
from typing import Annotated, Literal, Self, get_args

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

type SupportedItemKind = Literal["user_message", "assistant_message", "function_call", "function_call_output", "reasoning", "skill_outcome"]  # noqa: E501

_SUPPORTED_ITEM_KINDS = frozenset(get_args(SupportedItemKind.__value__))


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
_MEDIA_COMPONENT = r"[A-Za-z0-9](?:[A-Za-z0-9!#$&^_.+-]*[A-Za-z0-9])?"
MediaType = Annotated[str, Field(pattern=rf"^{_MEDIA_COMPONENT}/{_MEDIA_COMPONENT}$")]

_BASE64_PATTERN = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")


def _decoded_base64_size(content: str) -> int:
    """不分配 decoded bytes 地校验 base64 形状并计算精确长度。"""
    if not _BASE64_PATTERN.fullmatch(content):
        raise ValueError("attachment content is not strict base64")
    padding = len(content) - len(content.rstrip("="))
    return len(content) // 4 * 3 - padding


class PayloadModel(JournalModel):
    """V1 业务 payload 的统一冻结、禁 extra 基类。"""

    payload_version: Literal[1] = 1


class PayloadModelV2(JournalModel):
    """显式升级业务 payload 的 V2 冻结基类。"""

    payload_version: Literal[2] = 2


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


class StableErrorV1(PayloadModel):
    """不携带 traceback/repr/地址的稳定错误描述。"""

    code: NonEmptyStr
    class_name: NonEmptyStr
    failure_class: NonEmptyStr
    safe_message: str | None = None
    descriptor_hash: HashHex | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ApprovedSafeMessage:
    """调用方已显式审批、可进入 durable record 的消息。"""

    text: str


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
    kind: Literal["image_base64", "provider_encrypted_content"]


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


class _ProviderStateEnvelopeV1(JournalModel):
    """strict audit 可持久化的 provider 专属不透明状态 envelope。"""

    provider: NonEmptyStr
    protocol: NonEmptyStr
    item_type: NonEmptyStr
    payload: CanonicalMapping

    @model_validator(mode="after")
    def _validate_responses_reasoning_projection(self) -> Self:
        """OpenAI/Codex reasoning 只允许设计批准的白名单字段。"""
        identity = (self.provider, self.protocol, self.item_type)
        if identity not in {
            ("openai", "responses", "reasoning"),
            ("codex", "responses", "reasoning"),
        }:
            return self
        label = "Codex" if self.provider == "codex" else "OpenAI"
        allowed = {"id", "type", "encrypted_content", "summary", "status"}
        if unknown := set(self.payload) - allowed:
            raise ValueError(
                f"unsupported {label} reasoning state fields: {sorted(unknown)}"
            )
        if not isinstance(self.payload.get("id"), str) or not self.payload["id"]:
            raise ValueError(f"{label} reasoning state requires id")
        if self.payload.get("type") != "reasoning":
            raise ValueError(f"{label} reasoning state type must be reasoning")
        encrypted = self.payload.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            raise ValueError(f"{label} reasoning state requires encrypted_content")
        summary = self.payload.get("summary")
        if summary is not None and not isinstance(summary, list):
            raise ValueError(f"{label} reasoning state summary must be a list")
        status = self.payload.get("status")
        if status is not None and not isinstance(status, str):
            raise ValueError(f"{label} reasoning state status must be a string")
        return self


class _ReasoningItemPayload(JournalModel):
    """reasoning 的稳定 payload 形状。"""

    text: str
    summary: str
    provider_state: _ProviderStateEnvelopeV1 | None = None


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
    error_detail: None = None
    cost_tokens: NonNegativeInt | None = None
    cost_duration_ms: NonNegativeInt | None = None
    cost_iterations: NonNegativeInt | None = None
    ts_unix: NonNegativeInt | None = None


def _is_canonical_uint(value: str) -> bool:
    """只接受无前导零的 ASCII 非负整数。"""
    return value.isascii() and value.isdigit() and (value == "0" or value[0] != "0")


def _operation_kind(value: str) -> str | None:
    """按唯一 grammar 识别 simple/turn/llm/tool/skill operation。"""
    parts = value.split(":")
    if len(parts) == 1:
        return "simple" if parts[0] else None
    if len(parts) == 3:
        return "lifecycle" if parts[0] and parts[1:] == ["lifecycle", "end"] else None
    if len(parts) not in (4, 6, 8) or not all(parts[:2]):
        return None
    if parts[2] != "turn" or not _is_canonical_uint(parts[3]):
        return None
    if len(parts) == 4:
        return "turn"
    if len(parts) == 6 and parts[4] == "llm" and _is_canonical_uint(parts[5]):
        return "llm"
    if len(parts) == 6 and parts[4] == "tool" and parts[5]:
        return "tool"
    if len(parts) == 8 and parts[4] == "tool" and parts[5] and parts[6] == "skill" and parts[7]:
        return "skill"
    return None


@dataclass(frozen=True, slots=True)
class JournalIdentities:
    """一个 submission 的稳定 operation identity 派生器。"""

    session_id: str
    thread_id: str
    submission_id: str

    def __post_init__(self) -> None:
        """拒绝会产生模糊 identity 的空或含分隔符组件。"""
        components = (self.session_id, self.thread_id, self.submission_id)
        if any(not item for item in components):
            raise ValueError("journal identity components must be non-empty")
        if any(":" in item for item in components):
            raise ValueError("journal identity components must be delimiter-free")

    def _owns(self, operation_id: str, kind: str) -> bool:
        """检查 canonical operation 是否属于当前 thread/submission。"""
        parts = operation_id.split(":")
        return (
            _operation_kind(operation_id) == kind
            and parts[:2] == [self.thread_id, self.submission_id]
        )

    def turn(self, index: int) -> str:
        """构造 root/child turn identity。"""
        if index < 0:
            raise ValueError("turn index must be non-negative")
        return f"{self.thread_id}:{self.submission_id}:turn:{index}"

    def llm(self, turn_id: str, iteration: int) -> str:
        """构造 logical LLM call identity。"""
        if not self._owns(turn_id, "turn"):
            raise ValueError("LLM parent must be this identity's canonical turn")
        if iteration < 0:
            raise ValueError("LLM iteration must be non-negative")
        return f"{turn_id}:llm:{iteration}"

    def attempt(self, llm_operation_id: str, retry_ordinal: int) -> str:
        """构造真实 network attempt identity。"""
        if not self._owns(llm_operation_id, "llm"):
            raise ValueError("attempt parent must be this identity's canonical LLM operation")
        if retry_ordinal < 0:
            raise ValueError("retry ordinal must be non-negative")
        return f"{llm_operation_id}:attempt:{retry_ordinal}"

    def tool(self, turn_id: str, call_id: str) -> str:
        """构造 Tool call identity。"""
        if not self._owns(turn_id, "turn"):
            raise ValueError("tool parent must be this identity's canonical turn")
        if not call_id or ":" in call_id:
            raise ValueError("tool call id must be non-empty and delimiter-free")
        return f"{turn_id}:tool:{call_id}"

    def skill(self, tool_operation_id: str, target_skill_id: str) -> str:
        """构造 Skill dispatch identity。"""
        if not self._owns(tool_operation_id, "tool"):
            raise ValueError("skill parent must be this identity's canonical tool operation")
        if not target_skill_id or ":" in target_skill_id:
            raise ValueError("target skill id must be non-empty and delimiter-free")
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
    if not record_type or ":" in record_type:
        raise ValueError("record type must be non-empty and delimiter-free")
    operation_kind = _operation_kind(operation_id)
    if operation_kind is None:
        raise ValueError("operation id is not canonical")
    if attempt_id == "none":
        raise ValueError("attempt id 'none' is reserved")
    if attempt_id is not None:
        prefix = f"{operation_id}:attempt:"
        ordinal_text = attempt_id.removeprefix(prefix)
        valid_attempt = attempt_id.startswith(prefix) and _is_canonical_uint(ordinal_text)
        if operation_kind != "llm" or not valid_attempt:
            raise ValueError("attempt id must be canonical and belong to its LLM operation")
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
        payload: PayloadModel | PayloadModelV2,
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
        """把版本化 payload canonicalize，同时保留调用方提供的全部内容。"""
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


def _validated_item_metadata(metadata: object) -> dict[str, JsonValue]:
    """保留自由 metadata，同时严格校验 Responses 的三个保留键。"""
    canonical = _canonical_mapping(metadata)
    for key in ("llm_sample_id", "origin_llm_sample_id"):
        value = canonical.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{key} must be a non-empty string")
    output_index = canonical.get("provider_output_index")
    if output_index is not None and (
        isinstance(output_index, bool)
        or not isinstance(output_index, int)
        or output_index < 0
    ):
        raise ValueError("provider_output_index must be a non-negative integer")
    return canonical


def serialize_response_item(
    item: ResponseItem, *, source_record_id: str
) -> ConversationItemV1:
    """显式读取六个稳定 ResponseItem 字段，不使用通用 model_dump。"""
    kind = _supported_item_kind(str(item.kind))
    payload = _validated_item_payload(kind, item.payload)
    metadata = _validated_item_metadata(item.metadata)
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
        metadata=_validated_item_metadata(payload.metadata),
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
    attachments: tuple[AttachmentV1, ...] | list[AttachmentV1],
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
