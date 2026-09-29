"""审计 Session resume 的纯函数扫描：未结算 effect 判定与 root history 重建。

ADR 0025：effect 遵循「durable intent → 至多一次 live dispatch → durable outcome 或
UNKNOWN」。崩溃遗留的**未匹配 intent 一律视为 UNKNOWN**——effect 可能已经发生，也
可能没有；resume 不能替运维猜。已经 durable 落为 ``unknown`` 终态的 outcome 同理（写入
它的 Session 当时即已冻结）。

工具调用的这两类 UNKNOWN 可由恢复路径写一条 ``tool_recovery_committed``（ADR 0070）结算：
本扫描把它视为对应 intent（及被改判的 unknown outcome）的终态。其余未结算 effect 仍一律
fail closed。

本模块只读 committed envelopes，不做 IO，不依赖 Engine。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from taifeng.conversation.journal.records import (
    ConversationItemV1,
    SubmissionAcceptedV1,
    deserialize_response_item,
)
from taifeng.conversation.journal.recovery_records import (
    TOOL_RECOVERY_RECORD_TYPE,
    ToolRecoveryCommittedV1,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.models import ResponseItem

# 记录 durable 终态「结果未知」的 status 取值（LLM / Tool / Skill 三类枚举同值）。
_UNKNOWN_STATUS = "unknown"


@dataclass(frozen=True, slots=True)
class ResumedHistory:
    """从 Journal 重建的 root thread 对话项与投影 / 编号水位。"""

    items: tuple[ResponseItem, ...]
    first_seq: int | None
    last_seq: int
    next_turn_index: int


def _payload_ref(envelope: JournalEnvelope, key: str) -> str:
    """读取终态 record 指向 intent 的必填引用字段；缺失即 Journal 契约违约。"""
    value = envelope.payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{envelope.record_type} missing {key}: {envelope.record_id}")
    return value


@dataclass(slots=True)
class _Settlement:
    """各类终态记录引用的 intent，以及已 durable 为 unknown、尚未被恢复改判的终态。"""

    llm: set[str] = field(default_factory=set)
    tool: set[str] = field(default_factory=set)
    skill: set[str] = field(default_factory=set)
    applied: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)
    recovered_outcomes: set[str] = field(default_factory=set)


def _settled_references(envelopes: Sequence[JournalEnvelope]) -> _Settlement:
    """收集各类终态记录引用的 intent，以及已 durable 为 unknown 的终态 record。

    Raises:
        ValueError / pydantic.ValidationError: 终态记录缺引用或恢复记录形状违约。
    """
    settled = _Settlement()
    for envelope in envelopes:
        kind = envelope.record_type
        if kind == "llm_response_checkpoint":
            settled.llm.add(_payload_ref(envelope, "request_record_id"))
        elif kind == "tool_outcome_committed":
            settled.tool.add(_payload_ref(envelope, "intent_record_id"))
        elif kind == TOOL_RECOVERY_RECORD_TYPE:
            # 恢复结论是 intent 的终态；改判 unknown outcome 时该 outcome 也随之结算。
            # 新记录类型一律按 DTO 严格校验，形状违约即 Journal 不可信
            recovery = ToolRecoveryCommittedV1.model_validate(envelope.payload)
            settled.tool.add(recovery.intent_record_id)
            if recovery.outcome_record_id is not None:
                settled.recovered_outcomes.add(recovery.outcome_record_id)
            continue
        elif kind == "skill_dispatch_finished" and envelope.operation_id is not None:
            # skill 的 selected 与 finished 共享同一 skill operation identity
            settled.skill.add(envelope.operation_id)
        elif kind == "submission_applied":
            settled.applied.add(_payload_ref(envelope, "accepted_record_id"))
        else:
            continue
        # 已 durable 的 unknown 终态同样不可自动续跑
        if envelope.payload.get("status") == _UNKNOWN_STATUS:
            settled.unknown.add(envelope.record_id)
    settled.unknown -= settled.recovered_outcomes
    return settled


def find_unsettled_effects(envelopes: Sequence[JournalEnvelope]) -> tuple[str, ...]:
    """返回尚未结算、需要恢复处置的 record id（按 Journal seq 排序）。

    - ``llm_request_committed`` 没有对应 ``llm_response_checkpoint``；
    - ``tool_intent_committed`` 没有对应 ``tool_outcome_committed`` / ``tool_recovery_committed``；
    - ``skill_selected`` 没有同 operation 的 ``skill_dispatch_finished``；
    - ``submission_accepted`` 没有对应 ``submission_applied``；
    - 任一终态 record 的 ``status`` 为 ``unknown``，且未被 ``tool_recovery_committed`` 改判。
    """
    settled = _settled_references(envelopes)
    pending: list[str] = []
    for envelope in envelopes:
        kind = envelope.record_type
        unsettled = (
            (kind == "llm_request_committed" and envelope.record_id not in settled.llm)
            or (kind == "tool_intent_committed" and envelope.record_id not in settled.tool)
            or (kind == "skill_selected" and envelope.operation_id not in settled.skill)
            or (kind == "submission_accepted" and envelope.record_id not in settled.applied)
            or envelope.record_id in settled.unknown
        )
        if unsettled:
            pending.append(envelope.record_id)
    return tuple(pending)


def root_thread_id(envelopes: Sequence[JournalEnvelope]) -> str:
    """初始化 batch 第二条 ``thread_created`` 即 Session 的 root thread。"""
    if len(envelopes) < 3 or envelopes[1].record_type != "thread_created":
        raise ValueError("journal does not start with the initialization batch")
    thread_id = envelopes[1].thread_id
    if thread_id is None:
        raise ValueError("initialization thread_created has no thread_id")
    return thread_id


def rebuild_root_history(
    envelopes: Sequence[JournalEnvelope],
    thread_id: str,
) -> ResumedHistory:
    """按 Journal seq 重建 root thread 的已提交对话项与下一 turn index。"""
    items: list[ResponseItem] = []
    seqs: list[int] = []
    next_turn_index = 0
    for envelope in envelopes:
        if envelope.thread_id != thread_id:
            continue
        if envelope.record_type == "conversation_item":
            payload = ConversationItemV1.model_validate(envelope.payload)
            items.append(deserialize_response_item(payload))
            seqs.append(envelope.seq)
        elif envelope.record_type == "submission_accepted":
            accepted = SubmissionAcceptedV1.model_validate(envelope.payload)
            if accepted.turn_index is not None:
                # 新 Engine 的 turn index 接续已 durable 的最大值，保持单调
                next_turn_index = max(next_turn_index, accepted.turn_index + 1)
    return ResumedHistory(
        items=tuple(items),
        first_seq=seqs[0] if seqs else None,
        last_seq=seqs[-1] if seqs else 0,
        next_turn_index=next_turn_index,
    )


__all__ = [
    "ResumedHistory",
    "find_unsettled_effects",
    "rebuild_root_history",
    "root_thread_id",
]
