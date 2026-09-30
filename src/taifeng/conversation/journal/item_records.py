"""对话项（``ResponseItem``）的 Journal wire 形状与序列化。

从 ``records.py`` 原样搬出（W7.1 拆文件，零行为变更）；``records.py`` 原名再导出全部符号。
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from taifeng.conversation.journal.context_items import (
    CompactedItemPayload,
    SuspensionItemPayload,
    SystemInjectionItemPayload,
)
from taifeng.conversation.journal.models import JournalModel, JournalRecord, JsonValue, NonEmptyStr
from taifeng.conversation.journal.records_base import (
    _SUPPORTED_ITEM_KINDS,
    CanonicalList,
    CanonicalMapping,
    ConversationItemV1,
    JournalRecordFactory,
    NonNegativeInt,
    SupportedItemKind,
    _canonical_mapping,
)
from taifeng.conversation.models import ResponseItem


class _UserMessageItemPayload(JournalModel):
    """user_message 的稳定 payload 形状；``source`` / ``from_thread`` 仅 peer 消息有（ADR 0100）。"""

    text: str
    attachments: CanonicalList
    source: Literal["peer"] | None = None
    from_thread: NonEmptyStr | None = None


class _AssistantMessageItemPayload(JournalModel):
    """assistant_message 的稳定 payload 形状。"""

    text: str
    model: NonEmptyStr


class _FunctionCallItemPayload(JournalModel):
    """function_call 的稳定 payload 形状。

    ``extra_content`` 是 provider 专属的 tool_call 扩展（派发不解释，仅供落史
    回放）。模型 ``extra="forbid"``，缺这个字段会把带 extra_content 的合法
    function_call 判为非法 → 冻结整个 session（ADR 0037）。缺省时
    ``conversation/models.py`` 不写该键，故默认 None。
    """

    call_id: NonEmptyStr
    name: NonEmptyStr
    arguments: str
    extra_content: CanonicalMapping | None = None


class _FunctionCallOutputItemPayload(JournalModel):
    """function_call_output 的稳定 payload 形状。

    ``attachments`` 是已通过 admission 的图片附件列表；同上，缺字段会让一条带
    图片的工具结果直接冻结 session。缺省时不写该键，故默认 None。
    """

    call_id: NonEmptyStr
    output: str
    is_error: bool
    attachments: CanonicalList | None = None


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
    # thinking-passback：Chat 协议 provider 的不透明回传状态（如 Anthropic thinking 块）
    provider_reasoning: dict[str, JsonValue] | None = None


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
    elif kind == "compacted":
        model = CompactedItemPayload.model_validate(payload)
    elif kind == "system_injection":
        model = SystemInjectionItemPayload.model_validate(payload)
    elif kind == "suspension":
        model = SuspensionItemPayload.model_validate(payload)
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


__all__ = [
    "UnsupportedConversationItemError",
    "_AssistantMessageItemPayload",
    "_FunctionCallItemPayload",
    "_FunctionCallOutputItemPayload",
    "_ProviderStateEnvelopeV1",
    "_ReasoningItemPayload",
    "_SkillOutcomeItemPayload",
    "_UserMessageItemPayload",
    "_supported_item_kind",
    "_validated_item_metadata",
    "_validated_item_payload",
    "conversation_item_record",
    "deserialize_response_item",
    "serialize_response_item",
]
