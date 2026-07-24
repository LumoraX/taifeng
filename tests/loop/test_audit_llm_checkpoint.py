"""Journal-backed LLM response checkpoint 测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.records import LlmStatus, stable_error
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    ModelAttemptOutcome,
    ModelAttemptPermit,
)
from taifeng.llm.errors import ServerError
from taifeng.llm.providers.sim import SimClient, SimFault, SimTurn
from taifeng.loop.audit_support import AuditHealth, SessionAuditFrozenError
from taifeng.loop.cancellation import CancellationToken
from tests.loop.test_audit_llm import (
    _api_request,
    _attempt_request,
    _ObservedCore,
    _observer,
    _state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.conversation.journal.models import (
        JournalAck,
        JournalRecord,
        SessionLease,
    )
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest


@pytest.mark.anyio
async def test_checkpoint_record_preserves_attempt_identity_and_causation(
    tmp_path,
) -> None:
    """checkpoint 与 request 共用 attempt lineage，并返回 definite durable ack。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)
    permit = await observer.before_attempt(_attempt_request())
    outcome = ModelAttemptOutcome(
        permit=permit,
        status=LlmStatus.COMPLETE,
        normalized_items=(
            {"kind": "reasoning", "text": "why"},
            {"kind": "assistant", "text": "ok"},
        ),
        usage={"input_tokens": 3, "output_tokens": 2},
        provider_request_id="provider-request-1",
        stable_error=None,
    )

    checkpoint = await observer.after_attempt(outcome)
    committed = [envelope async for envelope in core.load("session_1")]
    record = committed[-1]

    assert checkpoint.operation_id == permit.operation_id
    assert checkpoint.attempt_id == permit.attempt_id
    assert checkpoint.request_record_id == permit.request_record_id
    assert checkpoint.retry_ordinal == permit.retry_ordinal
    assert checkpoint.checkpoint_record_id == record.record_id
    assert record.record_id == (
        f"{permit.operation_id}:llm_response_checkpoint:{permit.attempt_id}:0"
    )
    assert record.record_type == "llm_response_checkpoint"
    assert record.operation_id == permit.operation_id
    assert record.attempt_id == permit.attempt_id
    assert record.causation_id == permit.request_record_id
    assert record.payload == {
        "payload_version": 1,
        "request_record_id": permit.request_record_id,
        "retry_ordinal": 0,
        "status": "complete",
        "normalized_items": [
            {"kind": "reasoning", "text": "why"},
            {"kind": "assistant", "text": "ok"},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "provider_request_id": "provider-request-1",
        "stable_error": None,
    }


@pytest.mark.anyio
async def test_checkpoint_replay_is_idempotent_but_changed_payload_freezes(
    tmp_path,
) -> None:
    """相同 identity/content 可 replay；同 record id 改内容 fail closed。"""
    state, _ = await _state(tmp_path)

    async def complete(text: str):
        observer = _observer(state)
        permit = await observer.before_attempt(_attempt_request())
        return await observer.after_attempt(
            ModelAttemptOutcome(
                permit=permit,
                status=LlmStatus.COMPLETE,
                normalized_items=({"kind": "assistant", "text": text},),
                usage={"total_tokens": 1},
                provider_request_id=None,
                stable_error=None,
            )
        )

    first = await complete("same")
    replay = await complete("same")
    assert replay == first

    with pytest.raises(SessionAuditFrozenError):
        await complete("changed")
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_checkpoint_rejects_forged_request_lineage(tmp_path) -> None:
    """同 operation/attempt 下伪造 request record id 也必须 fail closed。"""
    state, _ = await _state(tmp_path)
    observer = _observer(state)
    permit = await observer.before_attempt(_attempt_request())
    forged = ModelAttemptPermit(
        operation_id=permit.operation_id,
        attempt_id=permit.attempt_id,
        request_record_id="forged-request-record",
        retry_ordinal=permit.retry_ordinal,
    )

    with pytest.raises(SessionAuditFrozenError):
        await observer.after_attempt(ModelAttemptOutcome(
            permit=forged,
            status=LlmStatus.COMPLETE,
            normalized_items=({"kind": "assistant", "text": "hidden"},),
            usage={"total_tokens": 1},
            provider_request_id=None,
            stable_error=None,
        ))

    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


class _NonCanonicalCompletedSession:
    """模拟 reviewed provider 意外产出非 canonical usage 的 runtime 边界。"""

    async def __aenter__(self) -> _NonCanonicalCompletedSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        del request
        from taifeng.llm.events import ResponseEvent, text_delta

        yield text_delta("must stay hidden")
        yield ResponseEvent(
            kind="completed",
            data={"usage": {"unsafe": object()}},
        )


@pytest.mark.anyio
async def test_runtime_outcome_dto_failure_writes_unknown_and_freezes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch 后 canonicalization 异常也必须 UNKNOWN/freeze/零发布。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)
    inner = SimClient(turns=[])
    monkeypatch.setattr(
        inner,
        "session",
        lambda **_: _NonCanonicalCompletedSession(),
    )
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    visible: list[str] = []

    with pytest.raises(SessionAuditFrozenError):
        await _consume(client, observer, visible)

    committed = [envelope async for envelope in core.load("session_1")]
    checkpoint = committed[-1]
    assert checkpoint.record_type == "llm_response_checkpoint"
    assert checkpoint.payload["status"] == "unknown"
    assert checkpoint.payload["normalized_items"] == [
        {"kind": "assistant", "text": "must stay hidden"}
    ]
    assert "object" not in str(checkpoint.payload)
    assert visible == []
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


@pytest.mark.anyio
async def test_error_checkpoint_ack_precedes_next_retry_request(
    tmp_path,
) -> None:
    """未来 retry 顺序必须是 request0/error0 ack/request1，且错误无 secret。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)
    first = await observer.before_attempt(_attempt_request())
    error = ServerError("secret provider body")
    error.request_id = "provider-request-error"
    checkpoint = await observer.after_attempt(
        ModelAttemptOutcome(
            permit=first,
            status=LlmStatus.ERROR,
            normalized_items=({"kind": "assistant", "text": "partial"},),
            usage=None,
            provider_request_id=error.request_id,
            stable_error=stable_error(error),
        )
    )
    second = await observer.before_attempt(_attempt_request())
    committed = [envelope async for envelope in core.load("session_1")]
    llm_records = committed[-3:]

    assert [record.record_type for record in llm_records] == [
        "llm_request_committed",
        "llm_response_checkpoint",
        "llm_request_committed",
    ]
    assert checkpoint.retry_ordinal == 0
    assert second.retry_ordinal == 1
    assert llm_records[1].payload["stable_error"]["code"] == "server_error"
    assert "secret provider body" not in str(llm_records[1].payload)


async def _consume(
    client: AttemptObservableClientAdapter,
    observer,
    visible: list[str],
    *,
    cancel: CancellationToken | None = None,
) -> None:
    """消费 observed stream，并记录 checkpoint barrier 后的可见 kind。"""
    session = client.session_with_attempt_observer(
        cancel=cancel or CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    async with session as entered:
        async for event in entered.stream(_api_request()):
            visible.append(event.kind)


class _CancelAfterRequestObserver:
    """在 request durable ack 之后、真实 dispatch 之前触发一次取消。

    用于锁死 spec「pre-dispatch 取消」语义：request intent 已 durable，但因未
    发生任何网络 attempt，故合法地不产生 checkpoint（与 Tool「每个 intent 恰好
    一个 outcome」的对称性差异，见 audit_llm.py retry_ordinal 注释）。
    """

    def __init__(self, inner, cancel: CancellationToken) -> None:
        """包裹真实 observer，并持有待在 permit 后取消的 session token。"""
        self._inner = inner
        self._cancel = cancel

    @property
    def finalization_timeout(self) -> float:
        """透传真实 observer 的 finalization 期限。"""
        return self._inner.finalization_timeout

    async def before_attempt(self, request):
        """先落 durable request intent，再在 dispatch 前触发取消。"""
        permit = await self._inner.before_attempt(request)
        self._cancel.cancel()
        return permit

    async def after_attempt(self, outcome):
        """dispatch 未发生时不应被调用；如被调用则如实委派。"""
        return await self._inner.after_attempt(outcome)


@pytest.mark.anyio
async def test_pre_dispatch_cancel_commits_request_without_checkpoint(
    tmp_path,
) -> None:
    """permit 后、dispatch 前取消：request durable、无 checkpoint、零 dispatch。"""
    state, core = await _state(tmp_path)
    token = CancellationToken(name="turn")
    observer = _CancelAfterRequestObserver(_observer(state), token)
    inner = SimClient(turns=[SimTurn(text="must stay hidden")])
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    visible: list[str] = []

    with pytest.raises(asyncio.CancelledError):
        await _consume(client, observer, visible, cancel=token)

    committed = [envelope async for envelope in core.load("session_1")]
    # request intent 已 durable 落账，且是最后一条 LLM 记录
    assert committed[-1].record_type == "llm_request_committed"
    # 未发生网络 attempt → 合法地没有 checkpoint 记录
    assert all(
        record.record_type != "llm_response_checkpoint" for record in committed
    )
    # dispatch spy 为零：底层 provider 从未收到请求
    assert inner.ledger.requests() == []
    # 无缓冲 delta 泄漏，且会话不因此 freeze（取消不是失败）
    assert visible == []
    assert state.coordinator.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_silent_stream_end_writes_unknown_and_freezes_without_events(
    tmp_path,
) -> None:
    """缺 terminal 的 dispatched attempt 必须 UNKNOWN/freeze/零事件可见。"""
    state, core = await _state(tmp_path)
    observer = _observer(state)
    inner = SimClient(
        turns=[
            SimTurn(
                text="partial",
                fault=SimFault.truncate_stream(after_events=3),
            )
        ]
    )
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    visible: list[str] = []

    with pytest.raises(SessionAuditFrozenError):
        await _consume(client, observer, visible)

    committed = [envelope async for envelope in core.load("session_1")]
    checkpoint = committed[-1]
    assert checkpoint.record_type == "llm_response_checkpoint"
    assert checkpoint.payload["status"] == "unknown"
    assert visible == []
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED


class _CheckpointCore(_ObservedCore):
    """只在 response checkpoint append 注入失败或无限阻塞。"""

    def __init__(self, *, fail_checkpoint: bool = False) -> None:
        super().__init__()
        self.fail_checkpoint = fail_checkpoint
        self.block_checkpoint = False
        self.checkpoint_entered = anyio.Event()
        self.checkpoint_exited = anyio.Event()

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """request 正常 durable；checkpoint 可受控失败/阻塞。"""
        if records[0].record_type == "llm_response_checkpoint":
            self.checkpoint_entered.set()
            try:
                if self.fail_checkpoint:
                    raise OSError("secret checkpoint storage detail")
                if self.block_checkpoint:
                    await anyio.sleep_forever()
            finally:
                self.checkpoint_exited.set()
        return await super().append_batch(
            records,
            lease=lease,
            expected_seq=expected_seq,
        )


@pytest.mark.anyio
async def test_checkpoint_append_failure_freezes_and_releases_no_events(
    tmp_path,
) -> None:
    """observer after_attempt 失败时 session freeze，缓冲 delta 全部丢弃。"""
    core = _CheckpointCore(fail_checkpoint=True)
    state, _ = await _state(tmp_path, core=core)
    observer = _observer(state)
    client = AttemptObservableClientAdapter(
        SimClient(turns=[SimTurn(text="must stay hidden")]),
        provider="sim",
        default_model="model-a",
    )
    visible: list[str] = []

    with pytest.raises(SessionAuditFrozenError):
        await _consume(client, observer, visible)

    assert visible == []
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED
    assert state.coordinator.effect_gate_open is False


@pytest.mark.anyio
async def test_checkpoint_timeout_is_bounded_and_freezes_without_worker_leak(
    tmp_path,
) -> None:
    """无限 append 必须按注入期限失败、freeze，且不遗留 checkpoint worker。"""
    core = _CheckpointCore()
    core.block_checkpoint = True
    state, real_core = await _state(
        tmp_path,
        core=core,
        finish_timeout=0.02,
    )
    observer = _observer(state)
    client = AttemptObservableClientAdapter(
        SimClient(turns=[SimTurn(text="must stay hidden")]),
        provider="sim",
        default_model="model-a",
    )
    visible: list[str] = []

    with anyio.fail_after(1):
        with pytest.raises(SessionAuditFrozenError):
            await _consume(client, observer, visible)

    await anyio.lowlevel.checkpoint()
    committed = [envelope async for envelope in real_core.load("session_1")]
    assert visible == []
    assert core.checkpoint_exited.is_set()
    assert all(record.record_type != "llm_response_checkpoint" for record in committed)
    assert all(
        not task.get_name().startswith("llm-checkpoint:")
        for task in asyncio.all_tasks()
        if not task.done()
    )
    assert state.coordinator.health is AuditHealth.RECOVERY_REQUIRED
