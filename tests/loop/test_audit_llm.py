"""Journal-backed LLM attempt observer 测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.models import (
    RootThreadDescriptor,
    SessionDescriptor,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.journal.records import ConversationItemV1, LlmStatus
from taifeng.conversation.models import (
    assistant_message,
    function_call,
    reasoning,
    user_message,
)
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    ModelAttemptOutcome,
    ModelAttemptRequest,
)
from taifeng.llm.providers.sim import RoutingSimClient, SimClient, SimTurn
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.audit import SessionAuditCoordinator
from taifeng.loop.audit_bootstrap import AuditedSessionState
from taifeng.loop.audit_llm import (
    JournalModelAttemptObserver,
    commit_audited_llm_response,
)
from taifeng.loop.audit_support import AuditHealth, SessionAuditFrozenError
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import UserMessage
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.conversation.journal.models import (
        JournalAck,
        JournalEnvelope,
        JournalRecord,
        SessionCreateResult,
        SessionLease,
    )


def _api_request(text: str = "hello") -> ApiRequest:
    """构造 observer 应完整留痕的 provider-neutral 请求。"""
    return ApiRequest(
        model="model-a",
        system_prompt=["system"],
        messages=[ApiMessage(role="user", content=text)],
        metadata={"trace": "stable"},
    )


def _attempt_request(text: str = "hello") -> ModelAttemptRequest:
    """构造 adapter 传给 Journal observer 的 immutable 请求。"""
    request = _api_request(text)
    return ModelAttemptRequest(
        provider="provider-a",
        model=request.model,
        api_request=request.model_dump(mode="json"),
    )


async def _state(
    tmp_path: Path,
    *,
    core: object | None = None,
    finish_timeout: float = 30.0,
) -> tuple[AuditedSessionState, JsonlSessionJournalCore]:
    """建立带真实初始化 batch 的最小 audited state。"""
    real_core = JsonlSessionJournalCore(tmp_path / "journal")
    selected = core or real_core
    if core is not None:
        core.inner = real_core  # type: ignore[attr-defined]
    created = await selected.create_session(  # type: ignore[attr-defined]
        SessionDescriptor(
            session_id="session_1",
            creation_operation_id="create_1",
            writer_id="writer_1",
            root_thread=RootThreadDescriptor(
                thread_id="thread_1",
                entry_skill_id="entry",
            ),
            config={},
        )
    )
    coordinator = SessionAuditCoordinator(
        core=selected,  # type: ignore[arg-type]
        lease=created.lease,
        expected_seq=created.ack.last_seq,
        finish_timeout=finish_timeout,
    )
    state = AuditedSessionState(
        thread_id="thread_1",
        coordinator=coordinator,
        projector=JournalConversationProjector(
            JsonlMessageStore(tmp_path / "threads")
        ),
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
    )
    return state, real_core


def _observer(
    state: AuditedSessionState,
    *,
    cancel: CancellationToken | None = None,
) -> JournalModelAttemptObserver:
    """构造固定 logical LLM operation 的 Journal observer。"""
    return JournalModelAttemptObserver(
        state=state,
        thread_id="thread_1",
        submission_id="submission_1",
        turn_index=3,
        iteration=2,
        cancel=cancel or CancellationToken(name="turn"),
    )


@pytest.mark.anyio
async def test_attempt_identity_increments_only_after_durable_ack(
    tmp_path: Path,
) -> None:
    """ordinal 从零连续递增，record/operation/attempt identity 均稳定。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)

    first = await observer.before_attempt(_attempt_request())
    second = await observer.before_attempt(_attempt_request())
    committed = [envelope async for envelope in core.load("session_1")]
    requests = committed[-2:]

    operation_id = "thread_1:submission_1:turn:3:llm:2"
    assert [first.retry_ordinal, second.retry_ordinal] == [0, 1]
    assert first.operation_id == second.operation_id == operation_id
    assert first.attempt_id == f"{operation_id}:attempt:0"
    assert second.attempt_id == f"{operation_id}:attempt:1"
    assert first.request_record_id == (
        f"{operation_id}:llm_request_committed:{first.attempt_id}:0"
    )
    assert second.request_record_id == (
        f"{operation_id}:llm_request_committed:{second.attempt_id}:0"
    )
    assert [item.record_id for item in requests] == [
        first.request_record_id,
        second.request_record_id,
    ]
    assert [item.attempt_id for item in requests] == [
        first.attempt_id,
        second.attempt_id,
    ]


@pytest.mark.anyio
async def test_concurrent_before_attempt_calls_receive_distinct_ordinals(
    tmp_path: Path,
) -> None:
    """误并发调用也必须串行化为 0/1，不能 historical replay 同一 attempt。"""
    state, _ = await _state(tmp_path)
    observer = _observer(state)
    permits = []

    async def observe() -> None:
        permits.append(await observer.before_attempt(_attempt_request()))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(observe)
        tasks.start_soon(observe)

    assert sorted(permit.retry_ordinal for permit in permits) == [0, 1]
    assert len({permit.attempt_id for permit in permits}) == 2


@pytest.mark.anyio
async def test_request_record_contains_complete_canonical_payload(
    tmp_path: Path,
) -> None:
    """request intent 保留完整 API request 和显式 effect/reconciliation。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)

    permit = await observer.before_attempt(_attempt_request())
    committed = [envelope async for envelope in core.load("session_1")]
    record = committed[-1]

    assert record.record_type == "llm_request_committed"
    assert record.operation_id == permit.operation_id
    assert record.attempt_id == permit.attempt_id
    assert record.thread_id == "thread_1"
    assert record.submission_id == "submission_1"
    assert record.turn_id == "thread_1:submission_1:turn:3"
    attempt = _attempt_request()
    assert record.payload == {
        "payload_version": 2,
        "turn_index": 3,
        "iteration": 2,
        "provider": "provider-a",
        "model": "model-a",
        "api_request_safe": _api_request().model_dump(mode="json"),
        "redactions": [],
        "canonical_attempt_sha256": attempt.canonical_attempt_sha256,
        "effect_kind": "external_non_idempotent",
        "idempotency_key": None,
        "reconciliation": "manual",
    }


class _ObservedCore:
    """为真实 core 注入 append barrier、失败或一次 prewrite cancel。"""

    def __init__(
        self,
        *,
        fail: BaseException | None = None,
        cancel_once: bool = False,
    ) -> None:
        self.inner: JsonlSessionJournalCore
        self.fail = fail
        self.cancel_once = cancel_once
        self.entered = anyio.Event()
        self.allow = anyio.Event()
        self.block = False
        self.events: list[str] = []

    async def create_session(
        self,
        descriptor: SessionDescriptor,
    ) -> SessionCreateResult:
        """委派真实初始化。"""
        return await self.inner.create_session(descriptor)

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """只在 LLM request append 注入受控行为。"""
        if records[0].record_type == "llm_request_committed":
            self.events.append("append_enter")
            self.entered.set()
            if self.cancel_once:
                self.cancel_once = False
                raise asyncio.CancelledError("prewrite cancelled")
            if self.fail is not None:
                raise self.fail
            if self.block:
                await self.allow.wait()
        ack = await self.inner.append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )
        if records[0].record_type == "llm_request_committed":
            self.events.append("ack")
        return ack

    def load(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[JournalEnvelope]:
        """委派 strict load。"""
        return self.inner.load(session_id, after_seq=after_seq)

    async def close_session(self, lease: SessionLease) -> None:
        """委派 lease close。"""
        await self.inner.close_session(lease)


async def _consume(
    client: AttemptObservableClientAdapter,
    observer: JournalModelAttemptObserver,
) -> None:
    """通过真实 adapter 消费一次受控 stream。"""
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    async with session as entered:
        async for _ in entered.stream(_api_request()):
            pass


@pytest.mark.anyio
async def test_real_adapter_dispatch_waits_for_journal_ack(tmp_path: Path) -> None:
    """append barrier 阻塞期间 dispatch spy 必须为零。"""
    core = _ObservedCore()
    state, _ = await _state(tmp_path, core=core)
    core.block = True
    observer = _observer(state)
    inner = SimClient(turns=[SimTurn(text="ok")])
    client = AttemptObservableClientAdapter(
        inner,
        provider="provider-a",
        default_model="model-a",
    )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_consume, client, observer)
        await core.entered.wait()
        assert inner.ledger.requests() == []
        core.allow.set()

    assert core.events == ["append_enter", "ack"]
    assert len(inner.ledger.requests()) == 1


@pytest.mark.anyio
async def test_append_failure_freezes_and_prevents_dispatch(tmp_path: Path) -> None:
    """Journal append 失败时 coordinator 冻结且 provider 未开始。"""
    core = _ObservedCore(fail=OSError("disk unavailable"))
    state, _ = await _state(tmp_path, core=core)
    observer = _observer(state)
    inner = SimClient(turns=[SimTurn(text="must not dispatch")])
    client = AttemptObservableClientAdapter(
        inner,
        provider="provider-a",
        default_model="model-a",
    )

    with pytest.raises(SessionAuditFrozenError):
        await _consume(client, observer)

    assert inner.ledger.requests() == []
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert state.coordinator.effect_gate_open is False


@pytest.mark.anyio
async def test_prewrite_cancel_does_not_consume_attempt_ordinal(
    tmp_path: Path,
) -> None:
    """明确 prewrite cancellation 后重试仍使用 ordinal 0。"""
    core = _ObservedCore(cancel_once=True)
    state, _ = await _state(tmp_path, core=core)
    observer = _observer(state)

    with pytest.raises(asyncio.CancelledError, match="prewrite cancelled"):
        await observer.before_attempt(_attempt_request())
    permit = await observer.before_attempt(_attempt_request())

    assert permit.retry_ordinal == 0
    assert permit.attempt_id.endswith(":attempt:0")
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_same_identity_changed_request_conflict_freezes(
    tmp_path: Path,
) -> None:
    """historical replay 只允许同内容；同 ID 改 request 必须冻结。"""
    state, _ = await _state(tmp_path)
    first_observer = _observer(state)
    replay_observer = _observer(state)
    changed_observer = _observer(state)

    first = await first_observer.before_attempt(_attempt_request("same"))
    replay = await replay_observer.before_attempt(_attempt_request("same"))
    with pytest.raises(SessionAuditFrozenError):
        await changed_observer.before_attempt(_attempt_request("changed"))

    assert replay == first
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_audited_turn_runner_injects_journal_observer(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """Engine 的 audited runner 必须走 observed session，而非 legacy session。"""
    inner = SimClient(turns=[SimTurn(text="observed")])
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="sim-model",
    )
    engine, _, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    runner = engine._new_turn_runner(  # noqa: SLF001
        "submission_1",
        CancellationToken(name="turn"),
        [],
    )
    runner.history_buffer.append(
        user_message("hello", thread_id="thr_audit_submission")
    )

    await runner._sample_once(0)  # noqa: SLF001

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    request, checkpoint, response, conversation = committed[-4:]
    assert request.record_type == "llm_request_committed"
    assert request.operation_id == (
        "thr_audit_submission:submission_1:turn:0:llm:0"
    )
    assert request.attempt_id == f"{request.operation_id}:attempt:0"
    assert checkpoint.record_type == "llm_response_checkpoint"
    assert checkpoint.operation_id == request.operation_id
    assert checkpoint.attempt_id == request.attempt_id
    assert checkpoint.causation_id == request.record_id
    # 7.6：最终逻辑响应在 checkpoint 之后 durable，引用 request/checkpoint lineage
    assert response.record_type == "llm_response_committed"
    assert response.operation_id == request.operation_id
    assert response.causation_id == checkpoint.record_id
    assert response.payload["request_record_id"] == request.record_id
    assert response.payload["checkpoint_record_id"] == checkpoint.record_id
    assert response.payload["status"] == "complete"
    # assistant 会话项与最终响应同批 durable，causation 指向 response
    assert conversation.record_type == "conversation_item"
    assert conversation.causation_id == response.record_id
    item = ConversationItemV1.model_validate(conversation.payload)
    assert item.item_kind == "assistant_message"
    assert item.payload["text"] == "observed"
    # 仅在 durable ack 后应用到 hot history；projection 单调推进到该会话项 seq
    assert runner.history_buffer[-1].kind == "assistant_message"
    assert runner.history_buffer[-1].payload["text"] == "observed"
    projected = engine._audit_state.projector.state(engine.thread_id)  # type: ignore[attr-defined]  # noqa: SLF001
    assert projected.projected_seq == conversation.seq
    assert projected.stale is False
    assert len(inner.ledger.requests()) == 1


async def _complete_checkpoint(state):
    """在真实 observer 上产出一个 COMPLETE attempt checkpoint。"""
    observer = _observer(state)
    permit = await observer.before_attempt(_attempt_request())
    return await observer.after_attempt(
        ModelAttemptOutcome(
            permit=permit,
            status=LlmStatus.COMPLETE,
            normalized_items=({"kind": "assistant", "text": "ok"},),
            usage={"total_tokens": 1},
            provider_request_id=None,
            stable_error=None,
        )
    )


@pytest.mark.anyio
async def test_audited_turn_provider_error_records_error_checkpoint_only(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """provider 错误：request + error checkpoint durable，无 response_committed。

    ConservativeFailurePolicy（默认）对 retryable ServerError 裁决 SYSTEM_RETRY 挂起；
    审计关键不变量是错误 attempt 已 durable 留痕、且未合成最终响应。
    """
    from taifeng.llm.providers.sim import SimFault
    from taifeng.suspend.signal import SuspendSignal

    inner = SimClient(turns=[SimTurn(text="", fault=SimFault.server_error())])
    client = AttemptObservableClientAdapter(
        inner, provider="sim", default_model="sim-model"
    )
    engine, _, core = await _engine_with_audit(
        tmp_path, skills_dir, model_client=client
    )
    runner = engine._new_turn_runner(  # noqa: SLF001
        "submission_1", CancellationToken(name="turn"), []
    )
    runner.history_buffer.append(
        user_message("hi", thread_id="thr_audit_submission")
    )

    with pytest.raises(SuspendSignal):
        await runner._sample_once(0)  # noqa: SLF001

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    request, checkpoint = committed[-2:]
    assert request.record_type == "llm_request_committed"
    assert checkpoint.record_type == "llm_response_checkpoint"
    assert checkpoint.payload["status"] == "error"
    # 未形成最终响应：无 llm_response_committed
    assert all(e.record_type != "llm_response_committed" for e in committed)


@pytest.mark.anyio
async def test_final_response_commits_ordered_conversation_items(
    tmp_path: Path,
) -> None:
    """最终响应批：llm_response_committed + provider 顺序会话项，projection 推进。"""
    state, core = await _state(tmp_path)
    await state.projector.bootstrap_thread(
        thread_id="thread_1",
        cwd=None,
        entry_skill_id="entry",
        source="session:session_1",
        extra={
            "audit_required": True,
            "journal_session_id": "session_1",
            "journal_schema_version": 1,
        },
    )
    checkpoint = await _complete_checkpoint(state)
    items = [
        reasoning("why", thread_id="thread_1"),
        assistant_message("ok", thread_id="thread_1", model="sim-model"),
        function_call("call_1", "do_it", "{}", thread_id="thread_1"),
    ]

    await commit_audited_llm_response(
        state=state,
        submission_id="submission_1",
        turn_index=3,
        iteration=2,
        checkpoint=checkpoint,
        items=items,
        cancel=CancellationToken(name="turn"),
    )

    committed = [envelope async for envelope in core.load("session_1")]
    response = committed[-4]
    conversation = committed[-3:]
    # 最终响应引用本 attempt 的 checkpoint lineage
    assert response.record_type == "llm_response_committed"
    assert response.operation_id == checkpoint.operation_id
    assert response.payload["checkpoint_record_id"] == checkpoint.checkpoint_record_id
    # 三条会话项严格 provider 顺序，ordinal 递增，causation 指向最终响应
    assert [envelope.record_type for envelope in conversation] == [
        "conversation_item"
    ] * 3
    kinds = [
        ConversationItemV1.model_validate(envelope.payload).item_kind
        for envelope in conversation
    ]
    assert kinds == ["reasoning", "assistant_message", "function_call"]
    assert all(envelope.causation_id == response.record_id for envelope in conversation)
    assert [envelope.record_id.rsplit(":", 1)[-1] for envelope in conversation] == [
        "0",
        "1",
        "2",
    ]
    # projection 单调推进到最后一条会话项 seq，且不 stale
    projected = state.projector.state("thread_1")
    assert projected.projected_seq == conversation[-1].seq
    assert projected.stale is False
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_sequential_audited_turns_keep_accepted_turn_indexes(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """按提交序串行完成的 accepted turns 沿用 admission 分配的 0/1 lineage。

    历史版本构造「反向完成」验证并发下 lineage 不串；ADR 0029 后根 turn 串行，
    第二轮在第一轮真终态后开跑，lineage 由 admission 顺序点决定、与执行时序无关。
    """
    inner = RoutingSimClient(
        routes={
            "SECOND_ATTEMPT": [SimTurn(text="second")],
            "FIRST_ATTEMPT": [SimTurn(text="first")],
        }
    )
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="sim-model",
    )
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    root_cancel = CancellationToken(name="sequential-root")
    actor = asyncio.create_task(engine.run(root_cancel))
    try:
        first_id = await engine.submit(UserMessage(text="FIRST_ATTEMPT"))
        second_id = await engine.submit(UserMessage(text="SECOND_ATTEMPT"))
        with anyio.fail_after(2):
            while (
                engine._turn_index < 2  # noqa: SLF001
                or coordinator.snapshot().accepted_work_ids
            ):
                await anyio.lowlevel.checkpoint()
    finally:
        root_cancel.cancel()
        await actor

    committed = [
        envelope
        async for envelope in core.load("ses_audit_submission")
        if envelope.record_type == "llm_request_committed"
    ]
    by_submission = {record.submission_id: record for record in committed}
    first = by_submission[first_id]
    second = by_submission[second_id]
    assert first.payload["turn_index"] == 0
    assert second.payload["turn_index"] == 1
    assert first.operation_id == (
        f"thr_audit_submission:{first_id}:turn:0:llm:1"
    )
    assert second.operation_id == (
        f"thr_audit_submission:{second_id}:turn:1:llm:1"
    )
