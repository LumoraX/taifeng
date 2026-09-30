"""Journal 业务记录的基类、稳定枚举、operation identity 与 record factory。

从 ``records.py`` 原样搬出（W7.1 拆文件，零行为变更）；``records.py`` 原名再导出全部符号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003  # Pydantic 运行期需要
from enum import StrEnum
from typing import Annotated, Literal, get_args

from pydantic import BeforeValidator, Field, field_validator

from taifeng.conversation.journal.canonical import model_canonical_data, validate_json_value
from taifeng.conversation.journal.models import (
    ActorRef,
    HashHex,
    JournalModel,
    JournalRecord,
    JsonValue,
    NonEmptyStr,
)

type SupportedItemKind = Literal["user_message", "assistant_message", "function_call", "function_call_output", "reasoning", "skill_outcome", "compacted", "system_injection", "suspension"]  # noqa: E501


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


_CONTEXT_OPERATIONS = frozenset(
    {"compaction", "budget_hint", "hook", "permission", "suspension"}
)


def _is_canonical_uint(value: str) -> bool:
    """只接受无前导零的 ASCII 非负整数。"""
    return value.isascii() and value.isdigit() and (value == "0" or value[0] != "0")


def _operation_kind(value: str) -> str | None:
    """按唯一 grammar 识别 simple/turn/llm/tool/skill/context operation。"""
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
    if len(parts) == 6 and parts[4] in _CONTEXT_OPERATIONS and _is_canonical_uint(parts[5]):
        return parts[4]
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

    def context(self, turn_id: str, kind: str, ordinal: int) -> str:
        """构造 turn 内上下文维护 operation 的 identity（压缩 / 预算提示）。"""
        if not self._owns(turn_id, "turn"):
            raise ValueError("context operation parent must be this identity's canonical turn")
        if kind not in _CONTEXT_OPERATIONS:
            raise ValueError(f"unknown context operation kind: {kind}")
        if ordinal < 0:
            raise ValueError("context operation ordinal must be non-negative")
        return f"{turn_id}:{kind}:{ordinal}"

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


__all__ = [
    "ApprovedSafeMessage",
    "CanonicalList",
    "CanonicalMapping",
    "ConversationItemV1",
    "JournalIdentities",
    "JournalRecordFactory",
    "LlmStatus",
    "MediaType",
    "NonNegativeInt",
    "PayloadModel",
    "PayloadModelV2",
    "SkillStatus",
    "StableErrorV1",
    "SupportedItemKind",
    "ToolStatus",
    "_CONTEXT_OPERATIONS",
    "_MEDIA_COMPONENT",
    "_SUPPORTED_ITEM_KINDS",
    "_canonical_list",
    "_canonical_mapping",
    "_decoded_base64_size",
    "_is_canonical_uint",
    "_operation_kind",
    "record_id",
]
