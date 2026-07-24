"""audit-required Turn 的 Journal-backed LLM attempt observer。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    LlmRequestCommittedV1,
)
from taifeng.llm.audit import (
    ModelAttemptPermit,
    ModelAttemptRequest,
)

if TYPE_CHECKING:
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken


class JournalModelAttemptObserver:
    """为一个 logical LLM operation 顺序签发 durable attempt permits。"""

    def __init__(
        self,
        *,
        state: AuditedSessionState,
        thread_id: str,
        submission_id: str,
        turn_index: int,
        iteration: int,
        cancel: CancellationToken,
    ) -> None:
        """冻结 logical identity；ordinal 只在 durable ack 后推进。"""
        if state.thread_id != thread_id:
            raise ValueError("audit LLM thread does not match Session root")
        self._state = state
        self._cancel = cancel
        self._turn_index = turn_index
        self._iteration = iteration
        self._identities = JournalIdentities(
            state.coordinator.session_id,
            thread_id,
            submission_id,
        )
        self._factory = JournalRecordFactory(
            session_id=state.coordinator.session_id,
            actor=ActorRef(kind="system", source="llm"),
            identities=self._identities,
        )
        self._turn_id = self._identities.turn(turn_index)
        self._operation_id = self._identities.llm(
            self._turn_id,
            iteration,
        )
        self._attempt_lock = anyio.Lock()
        self._next_retry_ordinal = 0

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        """durable 写完整 request intent，definite ack 后才消费 ordinal。"""
        async with self._attempt_lock:
            return await self._commit_attempt(request)

    async def _commit_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        """在 observer lock 内构造、提交并推进唯一 ordinal。"""
        retry_ordinal = self._next_retry_ordinal
        attempt_id = self._identities.attempt(
            self._operation_id,
            retry_ordinal,
        )
        record = self._factory.build(
            operation_id=self._operation_id,
            record_type="llm_request_committed",
            payload=LlmRequestCommittedV1(
                turn_index=self._turn_index,
                iteration=self._iteration,
                provider=request.provider,
                model=request.model,
                api_request=request.api_request_dict(),
                effect_kind="external_non_idempotent",
                idempotency_key=None,
                reconciliation="manual",
            ),
            attempt_id=attempt_id,
            submission_id=self._identities.submission_id,
            thread_id=self._identities.thread_id,
            turn_id=self._turn_id,
        )
        await self._state.coordinator.append(
            record,
            cancel=self._cancel,
        )
        self._next_retry_ordinal += 1
        return ModelAttemptPermit(
            operation_id=self._operation_id,
            attempt_id=attempt_id,
            request_record_id=record.record_id,
            retry_ordinal=retry_ordinal,
        )


__all__ = ["JournalModelAttemptObserver"]
