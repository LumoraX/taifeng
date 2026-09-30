"""审计 Session resume 时作废没有 checkpoint 的 LLM 请求（ADR 0103）。

进程死在 LLM 调用途中是最常见的崩溃时刻：一个 turn 的大部分时间都在等模型。此时 Journal 里
有 ``llm_request_committed`` 而没有任何 checkpoint——请求发出去了，回复一个字也没进过对话。

LLM 调用对内核没有外部副作用，作废它不会重复任何事情。接管时为每条这样的请求落一条
``llm_request_abandoned``，那个 turn 到此为止，模型在下一个 turn 继续。

本模块只做纯计算；记录的追加与全有或全无的语义由 ``audit_resume`` 负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import JournalIdentities, JournalRecordFactory
from taifeng.conversation.journal.recovery_records import (
    LLM_REQUEST_ABANDONED_RECORD_TYPE,
    LlmRequestAbandonedV1,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord

_ACTOR = ActorRef(kind="system", source="recovery")


def unsettled_llm_requests(
    envelopes: Sequence[JournalEnvelope],
    pending: Collection[str],
    threads: Collection[str],
) -> tuple[JournalEnvelope, ...]:
    """未结算 record 里、可收敛 thread 上的 LLM 请求，按落账顺序。"""
    return tuple(
        envelope for envelope in envelopes
        if envelope.record_type == "llm_request_committed"
        and envelope.record_id in pending
        and envelope.thread_id in threads
    )


def abandoned_request_record(
    request: JournalEnvelope, *, session_id: str, recovery_operation_id: str,
) -> JournalRecord:
    """一条被作废的 LLM 请求的结论记录。"""
    assert request.thread_id is not None
    assert request.submission_id is not None
    assert request.operation_id is not None
    factory = JournalRecordFactory(
        session_id=session_id,
        actor=_ACTOR,
        identities=JournalIdentities(session_id, request.thread_id, request.submission_id),
    )
    return factory.build(
        operation_id=request.operation_id,
        record_type=LLM_REQUEST_ABANDONED_RECORD_TYPE,
        payload=LlmRequestAbandonedV1(
            request_record_id=request.record_id,
            recovery_operation_id=recovery_operation_id,
        ),
        submission_id=request.submission_id,
        thread_id=request.thread_id,
        turn_id=request.turn_id,
        causation_id=request.record_id,
        correlation_id=recovery_operation_id,
    )


__all__ = ["abandoned_request_record", "unsettled_llm_requests"]
