"""audit-required Turn 的 Journal-backed LLM attempt observer。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import anyio

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    LlmRequestCommittedV1,
    LlmResponseCheckpointV1,
    LlmStatus,
    record_id,
)
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    ModelAttemptCheckpoint,
    ModelAttemptOutcome,
    ModelAttemptPermit,
    ModelAttemptRequest,
)

if TYPE_CHECKING:
    from taifeng.llm.client import ModelClient, ModelClientSession
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuditedTurnInput:
    """actor 从 durable UserMessage envelope 派生的 turn 输入。"""

    id: str
    text: str
    accepted_turn_index: int


class TurnModelContext(Protocol):
    """构造 legacy/audited model session 所需的最小 TurnRunner 视图。"""

    audit_state: AuditedSessionState | None
    model_client: ModelClient
    cancel: CancellationToken
    thread_id: str
    submission_id: str
    turn_index: int


def audited_turn_index(value: object) -> int | None:
    """从 audited turn 输入提取 admission index；legacy 返回 None。"""
    if isinstance(value, AuditedTurnInput):
        return value.accepted_turn_index
    return None


def model_session_for_turn(
    turn: TurnModelContext,
    iteration: int,
) -> ModelClientSession:
    """为 audited turn 注入 observer；legacy 原样创建普通 session。"""
    state = turn.audit_state
    if state is None:
        return turn.model_client.session(cancel=turn.cancel)
    client = turn.model_client
    if type(client) is not AttemptObservableClientAdapter:
        error = RuntimeError("audit model attempt observer unavailable")
        raise state.coordinator.freeze(error) from None
    observer = JournalModelAttemptObserver(
        state=state,
        thread_id=turn.thread_id,
        submission_id=turn.submission_id,
        turn_index=turn.turn_index,
        iteration=iteration,
        cancel=turn.cancel,
    )
    return client.session_with_attempt_observer(
        cancel=turn.cancel,
        attempt_observer=observer,
    )


def record_model_cache_read(client: ModelClient, value: int) -> None:
    """安全转发可选 cache-read telemetry，不影响 turn 主流程。"""
    recorder = getattr(client, "record_cache_read", None)
    if not callable(recorder):
        return
    try:
        recorder(value)
    except Exception:
        logger.debug("record_cache_read on client failed", exc_info=True)


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

    @property
    def finalization_timeout(self) -> float:
        """复用 Session coordinator 注入的 bounded finalization 期限。"""
        return self._state.coordinator.finalization_timeout

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        """durable 写完整 request intent，definite ack 后才消费 ordinal。"""
        async with self._attempt_lock:
            return await self._commit_attempt(request)

    async def after_attempt(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        """durable 写 attempt checkpoint，并仅在 definite ack 后返回 lineage。"""
        try:
            async with self._attempt_lock:
                checkpoint = await self._commit_checkpoint(outcome)
        except (KeyboardInterrupt, SystemExit) as error:
            self._state.coordinator.freeze(error)
            raise
        except BaseException as error:
            raise self._state.coordinator.freeze(error) from None
        if outcome.status is LlmStatus.UNKNOWN:
            assert outcome.stable_error is not None
            raise self._state.coordinator.freeze(outcome.stable_error) from None
        return checkpoint

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

    async def _commit_checkpoint(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        """构造 attempt-specific checkpoint，并等待 exact durable ack。"""
        permit = outcome.permit
        if permit.operation_id != self._operation_id:
            raise self._state.coordinator.freeze(
                ValueError("attempt operation does not match observer")
            )
        expected_attempt = self._identities.attempt(
            self._operation_id,
            permit.retry_ordinal,
        )
        if permit.attempt_id != expected_attempt:
            raise self._state.coordinator.freeze(
                ValueError("attempt identity does not match observer")
            )
        expected_request_record_id = record_id(
            permit.operation_id,
            "llm_request_committed",
            permit.attempt_id,
            0,
        )
        if permit.request_record_id != expected_request_record_id:
            raise self._state.coordinator.freeze(
                ValueError("attempt request lineage does not match observer")
            )
        record = self._factory.build(
            operation_id=permit.operation_id,
            record_type="llm_response_checkpoint",
            payload=LlmResponseCheckpointV1(
                request_record_id=permit.request_record_id,
                retry_ordinal=permit.retry_ordinal,
                status=outcome.status,
                normalized_items=outcome.normalized_items_list(),
                usage=outcome.usage_dict(),
                provider_request_id=outcome.provider_request_id,
                stable_error=outcome.stable_error,
            ),
            attempt_id=permit.attempt_id,
            submission_id=self._identities.submission_id,
            thread_id=self._identities.thread_id,
            turn_id=self._turn_id,
            causation_id=permit.request_record_id,
        )
        await self._state.coordinator.append(record)
        return ModelAttemptCheckpoint(
            operation_id=permit.operation_id,
            attempt_id=permit.attempt_id,
            request_record_id=permit.request_record_id,
            checkpoint_record_id=record.record_id,
            retry_ordinal=permit.retry_ordinal,
            status=outcome.status,
            normalized_items=outcome.normalized_items,
            usage=outcome.usage,
            provider_request_id=outcome.provider_request_id,
            stable_error=outcome.stable_error,
        )


__all__ = [
    "AuditedTurnInput",
    "JournalModelAttemptObserver",
    "audited_turn_index",
    "model_session_for_turn",
    "record_model_cache_read",
]
