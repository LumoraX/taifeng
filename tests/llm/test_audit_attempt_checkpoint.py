"""LLM attempt checkpoint barrier 的严格时序测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.conversation.journal.records import LlmStatus
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    AttemptObservedModelClientSession,
    ModelAttemptCheckpoint,
    ModelAttemptOutcome,
    ModelAttemptPermit,
    ModelAttemptRequest,
)
from taifeng.llm.errors import ServerError
from taifeng.llm.events import (
    completed,
    normalized_output,
    prompt_cache,
    reasoning_delta,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers.sim import SimClient
from taifeng.llm.types import (
    ApiMessage,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    TokenUsage,
)
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.events import ResponseEvent


def _request() -> ApiRequest:
    """构造最小 provider-neutral 请求。"""
    return ApiRequest(
        model="model-a",
        messages=[ApiMessage(role="user", content="hello")],
    )


def _permit() -> ModelAttemptPermit:
    """构造 request intent 已 durable 的 permit。"""
    operation_id = "thread_1:submission_1:turn:0:llm:0"
    attempt_id = f"{operation_id}:attempt:0"
    return ModelAttemptPermit(
        operation_id=operation_id,
        attempt_id=attempt_id,
        request_record_id=(f"{operation_id}:llm_request_committed:{attempt_id}:0"),
        retry_ordinal=0,
    )


def test_audit_prefers_terminal_responses_normalized_output() -> None:
    """strict checkpoint 必须保留 Responses terminal items 与 encrypted state。"""
    from taifeng.llm.audit import _normalized_items

    terminal = [
        {
            "type": "reasoning",
            "output_index": 0,
            "visible_text": "summary",
            "state": {
                "provider": "openai",
                "protocol": "responses",
                "item_type": "reasoning",
                "payload": {"encrypted_content": "ciphertext"},
            },
        }
    ]

    assert _normalized_items([normalized_output(terminal)]) == tuple(terminal)


class _CompletedSession:
    """先完整产出 provider events，再标记底层 stream 已结束。"""

    def __init__(self) -> None:
        self.ended = anyio.Event()

    async def __aenter__(self) -> _CompletedSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        del request
        yield reasoning_delta("why")
        yield text_delta("hello")
        yield tool_call_delta("call-1", "search", '{"q":')
        yield prompt_cache(cache_read=2, cache_creation=1)
        yield tool_call_delta("call-1", "search", '"x"}')
        yield tool_call_done("call-1", "search", '{"q":"x"}')
        yield completed(
            response_id="response-1",
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            request_id="provider-request-1",
        )
        self.ended.set()


class _CheckpointBarrierObserver:
    """阻塞 response checkpoint ack，观察 visible event barrier。"""

    finalization_timeout = 1.0

    def __init__(self) -> None:
        self.after_entered = anyio.Event()
        self.allow_ack = anyio.Event()
        self.acked = anyio.Event()
        self.outcomes: list[object] = []
        self.requests: list[ModelAttemptRequest] = []

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        self.requests.append(request)
        return _permit()

    async def after_attempt(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        self.outcomes.append(outcome)
        self.after_entered.set()
        await self.allow_ack.wait()
        self.acked.set()
        return ModelAttemptCheckpoint(
            operation_id=outcome.permit.operation_id,
            attempt_id=outcome.permit.attempt_id,
            request_record_id=outcome.permit.request_record_id,
            checkpoint_record_id=(
                f"{outcome.permit.operation_id}:llm_response_checkpoint:"
                f"{outcome.permit.attempt_id}:0"
            ),
            retry_ordinal=outcome.permit.retry_ordinal,
            status=outcome.status,
            normalized_items=outcome.normalized_items,
            usage=outcome.usage,
            provider_request_id=outcome.provider_request_id,
            stable_error=outcome.stable_error,
        )


class _IncompleteObserver:
    """缺 response checkpoint 能力的静态不完整 observer。"""

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        del request
        return _permit()


def test_incomplete_observer_is_rejected_before_session_or_dispatch() -> None:
    """缺 after_attempt/timeout 的 observer 必须在构造 observed session 时拒绝。"""
    inner = SimClient(turns=[])
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )

    with pytest.raises(TypeError, match="checkpoint protocol"):
        client.session_with_attempt_observer(
            cancel=CancellationToken(name="turn"),
            attempt_observer=_IncompleteObserver(),  # type: ignore[arg-type]
        )

    assert inner.ledger.requests() == []


@pytest.mark.anyio
async def test_complete_checkpoint_ack_precedes_all_visible_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpoint 未 ack 时零事件可见，ack 后按 provider 原序释放。"""
    inner = SimClient(turns=[])
    provider_session = _CompletedSession()
    monkeypatch.setattr(inner, "session", lambda **_: provider_session)
    observer = _CheckpointBarrierObserver()
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    session: AttemptObservedModelClientSession = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    visible: list[str] = []

    async def consume() -> None:
        async with session as entered:
            async for event in entered.stream(_request()):
                visible.append(event.kind)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        await provider_session.ended.wait()
        assert visible == []
        with anyio.fail_after(1):
            await observer.after_entered.wait()
        observer.allow_ack.set()

    assert visible == [
        "reasoning_delta",
        "text_delta",
        "tool_call_delta",
        "prompt_cache",
        "tool_call_delta",
        "tool_call_done",
        "completed",
    ]
    assert len(observer.outcomes) == 1
    outcome = observer.outcomes[0]
    assert isinstance(outcome, ModelAttemptOutcome)
    assert outcome.normalized_items_list() == [
        {"kind": "reasoning", "text": "why"},
        {"kind": "assistant", "text": "hello"},
        {
            "kind": "function_call",
            "call_id": "call-1",
            "name": "search",
            "arguments": '{"q":"x"}',
        },
    ]
    assert outcome.usage_dict() == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_tokens": 0,
        "raw": {},
    }
    assert outcome.provider_request_id == "provider-request-1"
    checkpoint = session.last_attempt_checkpoint
    assert checkpoint is not None
    assert checkpoint.checkpoint_record_id.endswith(
        f":llm_response_checkpoint:{checkpoint.attempt_id}:0"
    )


@pytest.mark.anyio
async def test_attempt_observer_receives_redacted_sensitive_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strict audit observer 不得取得图片正文或 Responses 加密状态。"""
    inner = SimClient(turns=[])
    provider_session = _CompletedSession()
    monkeypatch.setattr(inner, "session", lambda **_: provider_session)
    observer = _CheckpointBarrierObserver()
    client = AttemptObservableClientAdapter(
        inner,
        provider="openai",
        default_model="gpt-5.6",
    )
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    image_body = "aW1hZ2UtYm9keQ=="
    request = ApiRequest(
        model="gpt-5.6",
        input_items=[
            ApiMessageItem(
                role="user",
                content=[
                    ImagePart(
                        media_type="image/png",
                        base64_data=image_body,
                        size=10,
                        sha256="0" * 64,
                    )
                ],
            ),
            ApiProviderStateItem(
                sample_id="sample-1",
                output_index=0,
                state=ProviderStateEnvelope(
                    provider="openai",
                    protocol="responses",
                    item_type="reasoning",
                    payload={"id": "rs_1", "encrypted_content": "ciphertext"},
                ),
            ),
        ],
    )

    async def consume() -> None:
        async with session as entered:
            async for _ in entered.stream(request):
                pass

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        await observer.after_entered.wait()
        captured = observer.requests[0].api_request_dict()
        observer.allow_ack.set()

    encoded = str(captured)
    assert image_body not in encoded
    assert "ciphertext" not in encoded
    assert "encrypted_content" not in encoded


class _ErrorSession(_CompletedSession):
    """产出 partial delta 后抛 provider error。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        del request
        yield text_delta("partial")
        raise ServerError("secret-token=do-not-persist")


@pytest.mark.anyio
async def test_error_checkpoint_ack_precedes_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """partial delta 不可见，error checkpoint ack 后才传播 provider 异常。"""
    inner = SimClient(turns=[])
    monkeypatch.setattr(inner, "session", lambda **_: _ErrorSession())
    observer = _CheckpointBarrierObserver()
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    visible: list[str] = []
    caught: list[BaseException] = []

    async def consume() -> None:
        try:
            async with session as entered:
                async for event in entered.stream(_request()):
                    visible.append(event.kind)
        except BaseException as error:
            caught.append(error)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        with anyio.fail_after(1):
            await observer.after_entered.wait()
        assert visible == []
        assert caught == []
        outcome = observer.outcomes[0]
        assert isinstance(outcome, ModelAttemptOutcome)
        assert outcome.status is LlmStatus.ERROR
        assert outcome.normalized_items_list() == [{"kind": "assistant", "text": "partial"}]
        assert outcome.stable_error is not None
        assert outcome.stable_error.code == "server_error"
        assert "secret-token" not in outcome.stable_error.model_dump_json()
        observer.allow_ack.set()

    assert len(caught) == 1
    assert isinstance(caught[0], ServerError)


class _CancelledSession(_CompletedSession):
    """dispatch 后保持阻塞，供 caller 原生取消真实 stream。"""

    def __init__(self) -> None:
        super().__init__()
        self.dispatched = anyio.Event()

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        del request
        yield text_delta("partial")
        self.dispatched.set()
        await anyio.sleep_forever()


class _TokenCancelledSession(_CompletedSession):
    """在 dispatch 后显式检查 turn token 的 provider session。"""

    def __init__(self, cancel: CancellationToken) -> None:
        super().__init__()
        self.cancel = cancel
        self.dispatched = anyio.Event()
        self.check_cancel = anyio.Event()

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        del request
        yield text_delta("partial")
        self.dispatched.set()
        await self.check_cancel.wait()
        self.cancel.raise_if_cancelled()
        raise AssertionError("cancelled token did not raise")


@pytest.mark.anyio
async def test_turn_token_cancel_after_dispatch_waits_for_checkpoint_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turn token 取消也须先完成 cancelled checkpoint，再原样传播取消。"""
    inner = SimClient(turns=[])
    cancel = CancellationToken(name="turn")
    provider_session = _TokenCancelledSession(cancel)
    monkeypatch.setattr(inner, "session", lambda **_: provider_session)
    observer = _CheckpointBarrierObserver()
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    session = client.session_with_attempt_observer(
        cancel=cancel,
        attempt_observer=observer,
    )
    caught: list[BaseException] = []

    async def consume() -> None:
        try:
            async with session as entered:
                async for _ in entered.stream(_request()):
                    pytest.fail("cancelled attempt leaked a buffered event")
        except BaseException as error:
            caught.append(error)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        await provider_session.dispatched.wait()
        cancel.cancel()
        provider_session.check_cancel.set()
        await observer.after_entered.wait()
        assert caught == []
        observer.allow_ack.set()

    assert observer.acked.is_set()
    assert len(observer.outcomes) == 1
    outcome = observer.outcomes[0]
    assert isinstance(outcome, ModelAttemptOutcome)
    assert outcome.status is LlmStatus.CANCELLED
    assert len(caught) == 1
    assert isinstance(caught[0], asyncio.CancelledError)


@pytest.mark.anyio
async def test_repeated_raw_cancel_cannot_interrupt_checkpoint_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch 后重复 caller cancel 也须先 exactly-once checkpoint 再传播。"""
    inner = SimClient(turns=[])
    provider_session = _CancelledSession()
    monkeypatch.setattr(inner, "session", lambda **_: provider_session)
    observer = _CheckpointBarrierObserver()
    client = AttemptObservableClientAdapter(
        inner,
        provider="sim",
        default_model="model-a",
    )
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )

    async def consume() -> None:
        async with session as entered:
            async for _ in entered.stream(_request()):
                pytest.fail("cancelled attempt leaked a buffered event")

    task = asyncio.create_task(consume())
    await provider_session.dispatched.wait()
    task.cancel("first caller cancel")
    await observer.after_entered.wait()
    task.cancel("second caller cancel")
    await anyio.lowlevel.checkpoint()

    assert not task.done()
    assert len(observer.outcomes) == 1
    outcome = observer.outcomes[0]
    assert isinstance(outcome, ModelAttemptOutcome)
    assert outcome.status is LlmStatus.CANCELLED
    observer.allow_ack.set()

    with pytest.raises(asyncio.CancelledError, match="first caller cancel"):
        await task
    assert observer.acked.is_set()
    assert len(observer.outcomes) == 1
