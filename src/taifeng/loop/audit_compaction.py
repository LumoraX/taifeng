"""审计模式下的上下文维护落账：折叠式压缩与预算提示（ADR 0094）。

```text
压缩为摘要发起的每次 LLM 调用   llm_request_committed → llm_response_checkpoint → llm_response_committed
一次成功应用的压缩             context_compacted + conversation_item(compacted)      （同批）
一条预算提示                   budget_hint_injected + conversation_item(system_injection)（同批）
```

提交顺序与其余 effect 一致：Journal ack 之后才更新 hot history 与投影。

压缩发起的 LLM 调用各是一个独立的 logical LLM operation，iteration 从
``COMPACTION_LLM_ITERATION_BASE`` 起编号，与采样的 iteration 不相撞。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.context_records import (
    BUDGET_HINT_RECORD_TYPE,
    COMPACTION_LLM_ITERATION_BASE,
    CONTEXT_COMPACTED_RECORD_TYPE,
    BudgetHintInjectedV1,
    ContextCompactedV1,
)
from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    JournalIdentities,
    JournalRecordFactory,
    PayloadModel,
    conversation_item_record,
)
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.loop.audit_llm import JournalModelAttemptObserver, commit_audited_llm_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.client import ModelClient, ModelClientSession
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken


class _TrackedSession:
    """把压缩策略拿到的会话转发给受审计的会话，并留住它以便事后取 checkpoint。"""

    def __init__(self, inner: ModelClientSession) -> None:
        self.inner = inner

    def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """原样转发。"""
        return self.inner.stream(request)

    async def __aenter__(self) -> _TrackedSession:
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.inner.__aexit__(*exc)


class AuditedCompactionModel:
    """压缩期间的受审计 LLM 会话来源：每开一个会话就是一次独立落账的 LLM 调用。"""

    def __init__(
        self,
        *,
        state: AuditedSessionState,
        model_client: ModelClient,
        submission_id: str,
        turn_index: int,
        first_call_ordinal: int,
        cancel: CancellationToken,
    ) -> None:
        """
        Args:
            state: 所在 thread 的审计状态。
            model_client: 会话使用的客户端；须是受审计的 attempt-observable 适配器。
            submission_id / turn_index: 所属 turn。
            first_call_ordinal: 本 turn 此前已发起的压缩 LLM 调用数。
            cancel: 所属 turn 的取消 token。
        """
        self._state = state
        self._client = model_client
        self._submission_id = submission_id
        self._turn_index = turn_index
        self._next_ordinal = first_call_ordinal
        self._cancel = cancel
        self._calls: list[tuple[int, ModelClientSession]] = []

    @property
    def next_call_ordinal(self) -> int:
        """下一次压缩 LLM 调用的序号（供同一 turn 的后续压缩接续）。"""
        return self._next_ordinal

    def session(self, model: str | None = None) -> ModelClientSession:
        """开一个受审计的会话；请求与 checkpoint 由 observer 落账。"""
        client = self._client
        if type(client) is not AttemptObservableClientAdapter:
            raise self._state.coordinator.freeze(
                RuntimeError("audit model attempt observer unavailable")
            ) from None
        iteration = COMPACTION_LLM_ITERATION_BASE + self._next_ordinal
        self._next_ordinal += 1
        inner = client.session_with_attempt_observer(
            cancel=self._cancel,
            attempt_observer=JournalModelAttemptObserver(
                state=self._state,
                thread_id=self._state.thread_id,
                submission_id=self._submission_id,
                turn_index=self._turn_index,
                iteration=iteration,
                cancel=self._cancel,
            ),
            model=model,
        )
        self._calls.append((iteration, inner))
        return _TrackedSession(inner)

    async def commit_responses(self) -> tuple[str, ...]:
        """给每次已收敛的调用补上最终响应记录；返回它们的 request record id。

        没有产生 checkpoint 的会话（从未发出请求）没有可提交的内容。调用失败同样提交——
        失败的调用也是事实。
        """
        request_ids: list[str] = []
        for iteration, session in self._calls:
            checkpoint = getattr(session, "last_attempt_checkpoint", None)
            if checkpoint is None:
                continue
            await commit_audited_llm_response(
                state=self._state,
                submission_id=self._submission_id,
                turn_index=self._turn_index,
                iteration=iteration,
                checkpoint=checkpoint,
                items=(),
                cancel=self._cancel,
            )
            request_ids.append(checkpoint.request_record_id)
        self._calls.clear()
        return tuple(request_ids)


async def _commit_with_item(
    *,
    state: AuditedSessionState,
    submission_id: str,
    turn_index: int,
    kind: str,
    ordinal: int,
    record_type: str,
    payload: PayloadModel,
    item: ResponseItem,
    cancel: CancellationToken,
) -> None:
    """原子提交一条上下文 record 与它产生的对话项，ack 后推进投影。"""
    coordinator = state.coordinator
    await coordinator.ensure_effect_allowed()
    identities = JournalIdentities(coordinator.session_id, state.thread_id, submission_id)
    turn_id = identities.turn(turn_index)
    operation_id = identities.context(turn_id, kind, ordinal)
    factory = JournalRecordFactory(
        session_id=coordinator.session_id,
        actor=ActorRef(kind="system", source="context"),
        identities=identities,
    )
    record = factory.build(
        operation_id=operation_id,
        record_type=record_type,
        payload=payload,
        submission_id=submission_id,
        thread_id=state.thread_id,
        turn_id=turn_id,
    )
    item_record = conversation_item_record(
        factory,
        operation_id=operation_id,
        item=item,
        source_record_id=record.record_id,
        ordinal=0,
        submission_id=submission_id,
        turn_id=turn_id,
    )
    batch = (record, item_record)
    ack = await coordinator.append_batch(batch, cancel=cancel)
    envelopes = await coordinator.load_acknowledged(ack, batch)
    conversation = tuple(
        envelope for envelope in envelopes if envelope.record_type == "conversation_item"
    )
    try:
        result = await state.projector.project(conversation, ack)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise coordinator.freeze(error) from None
    coordinator.update_projection(result)


async def commit_audited_compaction(
    *,
    state: AuditedSessionState,
    submission_id: str,
    turn_index: int,
    payload: ContextCompactedV1,
    summary_item: ResponseItem,
    cancel: CancellationToken,
) -> None:
    """提交一次成功的压缩：``context_compacted`` 与摘要条目同批落账。

    Raises:
        SessionAuditFrozenError: Journal 写入不确定，Session 已冻结。
    """
    await _commit_with_item(
        state=state, submission_id=submission_id, turn_index=turn_index,
        kind="compaction", ordinal=payload.ordinal,
        record_type=CONTEXT_COMPACTED_RECORD_TYPE, payload=payload,
        item=summary_item, cancel=cancel,
    )


async def commit_audited_budget_hint(
    *,
    state: AuditedSessionState,
    submission_id: str,
    turn_index: int,
    ordinal: int,
    payload: BudgetHintInjectedV1,
    note: ResponseItem,
    cancel: CancellationToken,
) -> None:
    """提交一条预算提示：``budget_hint_injected`` 与提示条目同批落账。"""
    await _commit_with_item(
        state=state, submission_id=submission_id, turn_index=turn_index,
        kind="budget_hint", ordinal=ordinal,
        record_type=BUDGET_HINT_RECORD_TYPE, payload=payload,
        item=note, cancel=cancel,
    )


def superseded_item_ids(
    before: list[ResponseItem], after: list[ResponseItem],
) -> set[str]:
    """压缩使之不再属于 hot history 的条目 id（被折叠的）。"""
    kept: dict[str, Any] = {item.id: item for item in after}
    return {item.id for item in before if kept.get(item.id) != item}


__all__ = [
    "AuditedCompactionModel",
    "commit_audited_budget_hint",
    "commit_audited_compaction",
    "superseded_item_ids",
]
