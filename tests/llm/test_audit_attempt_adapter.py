"""一次网络 attempt 可观测 LLM client adapter 测试。"""

from __future__ import annotations

import asyncio
from collections import UserDict
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest

from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    ModelAttemptCheckpoint,
    ModelAttemptOutcome,
    ModelAttemptPermit,
    ModelAttemptRequest,
)
from taifeng.llm.client import OneNetworkAttemptModelClient
from taifeng.llm.events import completed, text_delta
from taifeng.llm.providers.anthropic_provider import AnthropicClient
from taifeng.llm.providers.deepseek_provider import DeepSeekClient
from taifeng.llm.providers.gemini_provider import GeminiClient
from taifeng.llm.providers.litellm_provider import LiteLLMClient
from taifeng.llm.providers.openai_compat import OpenAICompatClient
from taifeng.llm.providers.sim import SimClient
from taifeng.llm.types import ApiMessage, ApiRequest, TokenUsage
from taifeng.loop.audit_config import AttemptObservableModelClient
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from taifeng.llm.client import ModelClientSession
    from taifeng.llm.events import ResponseEvent


def _request(*, model: str = "") -> ApiRequest:
    """构造不依赖 provider 的最小请求。"""
    return ApiRequest(
        model=model,
        messages=[ApiMessage(role="user", content="hello")],
    )


def _permit(ordinal: int = 0) -> ModelAttemptPermit:
    """构造 durable observer 才能返回的稳定 permit。"""
    operation_id = "thread_1:submission_1:turn:0:llm:0"
    attempt_id = f"{operation_id}:attempt:{ordinal}"
    return ModelAttemptPermit(
        operation_id=operation_id,
        attempt_id=attempt_id,
        request_record_id=(
            f"{operation_id}:llm_request_committed:{attempt_id}:0"
        ),
        retry_ordinal=ordinal,
    )


class _DispatchSpySession:
    """在开始迭代真实 stream 时记录 observer ack 状态。"""

    def __init__(self, events: list[str], *, event_count: int = 1) -> None:
        self.events = events
        self.event_count = event_count
        self.dispatched = False
        self.exit_calls = 0
        self.requests: list[ApiRequest] = []

    async def __aenter__(self) -> _DispatchSpySession:
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        self.exit_calls += 1
        self.events.append("exit")

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        self.dispatched = True
        self.requests.append(request)
        self.events.append("dispatch")
        for index in range(self.event_count):
            yield text_delta(f"ok-{index}")
        yield completed(response_id=None, usage=TokenUsage())


class _OneAttemptClient(OneNetworkAttemptModelClient):
    """每个 session 只进行一次真实 stream dispatch 的受控 client。"""

    def __init__(self, events: list[str], *, event_count: int = 1) -> None:
        self.events = events
        self.event_count = event_count
        self.sessions: list[_DispatchSpySession] = []
        self.legacy_calls = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        del cancel, model
        self.legacy_calls += 1
        self.events.append("session_construct")
        session = _DispatchSpySession(
            self.events,
            event_count=self.event_count,
        )
        self.sessions.append(session)
        return session


def _reviewed_spy_inner(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    event_count: int = 1,
) -> tuple[SimClient, _OneAttemptClient]:
    """用 exact reviewed Sim 类型承载受控 session spy。"""
    reviewed = SimClient(turns=[])
    spy = _OneAttemptClient(events, event_count=event_count)
    monkeypatch.setattr(reviewed, "session", spy.session)
    return reviewed, spy


class _BlockingObserver:
    """显式 gate durable permit，便于证明 ack-before-dispatch。"""

    finalization_timeout = 1.0

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.entered = anyio.Event()
        self.allow = anyio.Event()
        self.requests: list[ModelAttemptRequest] = []

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        self.requests.append(request)
        self.entered.set()
        await self.allow.wait()
        self.events.append("ack")
        return _permit()

    async def after_attempt(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        """返回与 outcome exact 一致的受控 checkpoint ack。"""
        return _checkpoint(outcome)


class _FailingObserver:
    """在 durable permit 前失败。"""

    finalization_timeout = 1.0

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        del request
        raise RuntimeError("journal unavailable")

    async def after_attempt(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        """before 必失败，after 不应可达。"""
        raise AssertionError(f"unexpected outcome: {outcome.status}")


async def _consume(session: ModelClientSession, request: ApiRequest) -> list[str]:
    """完整消费 adapter stream。"""
    async with session as entered:
        return [
            str(event.data.get("text", ""))
            async for event in entered.stream(request)
            if event.kind == "text_delta"
        ]


def _checkpoint(outcome: ModelAttemptOutcome) -> ModelAttemptCheckpoint:
    """构造与 outcome exact 一致的 definite checkpoint。"""
    permit = outcome.permit
    return ModelAttemptCheckpoint(
        operation_id=permit.operation_id,
        attempt_id=permit.attempt_id,
        request_record_id=permit.request_record_id,
        checkpoint_record_id=(
            f"{permit.operation_id}:llm_response_checkpoint:{permit.attempt_id}:0"
        ),
        retry_ordinal=permit.retry_ordinal,
        status=outcome.status,
        normalized_items=outcome.normalized_items,
        usage=outcome.usage,
        provider_request_id=outcome.provider_request_id,
        stable_error=outcome.stable_error,
    )


@pytest.mark.anyio
async def test_durable_observer_ack_precedes_actual_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """observer barrier 未放行时底层 stream 必须完全未开始。"""
    events: list[str] = []
    reviewed, inner = _reviewed_spy_inner(monkeypatch, events)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver(events)
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )

    async with anyio.create_task_group() as tasks:
        result: list[str] = []

        async def run() -> None:
            result.extend(await _consume(session, _request()))

        tasks.start_soon(run)
        await observer.entered.wait()
        assert events == []
        assert inner.sessions == []
        observer.allow.set()

    assert result == ["ok-0"]
    assert events == ["ack", "session_construct", "enter", "dispatch", "exit"]
    assert inner.sessions[0].dispatched is True
    assert inner.sessions[0].exit_calls == 1
    assert observer.requests[0].provider == "provider-a"
    assert observer.requests[0].model == "model-a"
    assert observer.requests[0].api_request["model"] == "model-a"
    assert inner.sessions[0].requests[0].model == "model-a"


@pytest.mark.anyio
async def test_observer_failure_prevents_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 definite durable permit 时不得调用底层 stream。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [])
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=_FailingObserver(),
    )

    with pytest.raises(RuntimeError, match="journal unavailable"):
        await _consume(session, _request())

    assert inner.sessions == []


@pytest.mark.anyio
async def test_target_cancel_during_observer_barrier_prevents_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target token 在 durable permit 前取消时不得触发 provider。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [])
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver([])
    cancel = CancellationToken(name="turn")
    session = client.session_with_attempt_observer(
        cancel=cancel,
        attempt_observer=observer,
    )

    async with anyio.create_task_group() as tasks:
        caught: list[BaseException] = []

        async def run() -> None:
            try:
                await _consume(session, _request())
            except BaseException as error:
                caught.append(error)

        tasks.start_soon(run)
        await observer.entered.wait()
        cancel.cancel()
        observer.allow.set()

    assert len(caught) == 1
    assert isinstance(caught[0], asyncio.CancelledError)
    assert inner.sessions == []


@pytest.mark.anyio
async def test_raw_caller_cancel_during_observer_barrier_prevents_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """caller task 原生取消时 adapter 不吞取消且不触发 provider。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [])
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver([])
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    task = asyncio.create_task(_consume(session, _request()))
    await observer.entered.wait()

    task.cancel("caller cancelled")
    with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
        await task

    assert inner.sessions == []


def test_adapter_is_nominal_attempt_observable_client_and_legacy_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 adapter 通过 nominal gate，普通 session 原样委派。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [])
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    cancel = CancellationToken(name="legacy")

    assert AttemptObservableModelClient in type(client).__mro__
    assert client.session(cancel=cancel) is inner.sessions[0]
    assert inner.legacy_calls == 1


def test_attempt_dtos_are_frozen_and_adapter_names_must_be_stable() -> None:
    """attempt DTO 不可替换字段，provider/model 均需稳定非空名称。"""
    permit = _permit()
    with pytest.raises(FrozenInstanceError):
        permit.retry_ordinal = 2  # type: ignore[misc]

    inner = _OneAttemptClient([])
    with pytest.raises(ValueError, match="provider"):
        AttemptObservableClientAdapter(
            inner,
            provider="",
            default_model="model-a",
        )
    with pytest.raises(ValueError, match="model"):
        AttemptObservableClientAdapter(
            inner,
            provider="provider-a",
            default_model="",
        )


@pytest.mark.parametrize(
    "api_request",
    [
        UserDict({"model": "model-a"}),
        cast("Any", (("model", "model-a"),)),
        cast("Any", {1: "numeric-key"}),
        cast("Any", {1: "numeric", "1": "string"}),
        {"model": "model-a", "messages": ()},
        {"model": "model-a", "temperature": float("nan")},
        {"model": "model-a", "temperature": float("inf")},
        {"model": "model-a", "nested": UserDict({"value": 1})},
    ],
)
def test_attempt_request_rejects_non_plain_or_noncanonical_json(
    api_request: Any,
) -> None:
    """observer snapshot 只接受无歧义的纯 JSON dict/list 树。"""
    with pytest.raises(NonCanonicalValueError):
        ModelAttemptRequest(
            provider="provider-a",
            model="model-a",
            api_request=api_request,
        )


def test_attempt_request_deep_copies_before_freezing_nested_json() -> None:
    """caller 后续修改 nested dict/list 不得改变 durable request snapshot。"""
    original = {
        "model": "model-a",
        "metadata": {"tags": ["before"]},
    }
    request = ModelAttemptRequest(
        provider="provider-a",
        model="model-a",
        api_request=original,
    )

    original["metadata"]["tags"].append("after")  # type: ignore[union-attr]

    assert request.api_request["metadata"] == {"tags": ("before",)}
    assert request.api_request_dict()["metadata"] == {"tags": ["before"]}


class _UncontrolledMultiDispatchClient:
    """可在单个 stream 内自行 fallback 多 endpoint 的未认证 client。"""

    def __init__(self) -> None:
        self.session_calls = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        del cancel, model
        self.session_calls += 1
        return _DispatchSpySession([])


def test_adapter_rejects_uncontrolled_inner_before_session_construction() -> None:
    """duck ModelClient 即使形状兼容也不能把 adapter 当 observability 证明。"""
    inner = _UncontrolledMultiDispatchClient()

    with pytest.raises(TypeError, match="one-network-attempt"):
        AttemptObservableClientAdapter(
            inner,  # type: ignore[arg-type]
            provider="unsafe",
            default_model="unsafe-model",
        )

    assert inner.session_calls == 0


def test_adapter_rejects_unregistered_nominal_marker_client() -> None:
    """公开 marker 的普通继承不能自行取得 strict dispatch 资格。"""
    with pytest.raises(TypeError, match="reviewed one-attempt"):
        AttemptObservableClientAdapter(
            _OneAttemptClient([]),
            provider="unregistered",
            default_model="unregistered-model",
        )


def test_adapter_rejects_external_subclass_of_reviewed_client() -> None:
    """仓库外 subclass 即使继承官方实现也必须重新经过 conformance 审核。"""

    class _ExternalSimClient(SimClient):
        """模拟仓库外可能覆盖 session/stream 的 subclass。"""

    with pytest.raises(TypeError, match="reviewed one-attempt"):
        AttemptObservableClientAdapter(
            _ExternalSimClient(turns=[]),
            provider="external",
            default_model="external-model",
        )


def test_adapter_rejects_litellm_with_opaque_retry_and_fallback_surface() -> None:
    """LiteLLM 未显式实现受控 attempt 边界时必须 fail closed。"""
    inner = LiteLLMClient(
        model="openai/test",
        extra_params={"fallbacks": [{"model": "openai/other"}]},
    )

    with pytest.raises(TypeError, match="one-network-attempt"):
        AttemptObservableClientAdapter(
            inner,  # type: ignore[arg-type]
            provider="litellm",
            default_model="openai/test",
        )


@pytest.mark.parametrize(
    "client_type",
    [
        SimClient,
        OpenAICompatClient,
        AnthropicClient,
        GeminiClient,
        DeepSeekClient,
    ],
)
def test_reviewed_direct_clients_nominally_declare_one_attempt(
    client_type: type[object],
) -> None:
    """只有已审查直连实现和 Sim 明确继承受控 attempt 边界。"""
    assert OneNetworkAttemptModelClient in client_type.__mro__


def test_litellm_does_not_nominally_declare_one_attempt() -> None:
    """LiteLLM opaque fallback/retry surface 不能拥有 nominal marker。"""
    assert OneNetworkAttemptModelClient not in LiteLLMClient.__mro__


def test_reviewed_deepseek_exact_type_is_adapter_eligible() -> None:
    """仓库审查过的 DeepSeek 薄封装必须显式列入 exact conformance 集合。"""
    client = AttemptObservableClientAdapter(
        DeepSeekClient(api_key="test"),
        provider="deepseek",
        default_model="deepseek-chat",
    )

    assert type(client) is AttemptObservableClientAdapter


def test_adapter_forwards_cache_read_to_next_inner_session() -> None:
    """两轮 adapter session 间必须保留 provider previous_cache_read telemetry。"""
    inner = OpenAICompatClient(
        base_url="https://example.invalid",
        api_key="test",
        model="model-a",
    )
    client = AttemptObservableClientAdapter(
        inner,
        provider="openai-compatible",
        default_model="model-a",
    )

    first = client.session(cancel=CancellationToken(name="first"))
    client.record_cache_read(321)
    second = client.session(cancel=CancellationToken(name="second"))

    assert first._previous_cache_read == 0  # noqa: SLF001
    assert second._previous_cache_read == 321  # noqa: SLF001


def test_adapter_cache_read_proxy_is_safe_when_inner_has_no_telemetry() -> None:
    """无 record_cache_read 的 reviewed client 仍可安全使用统一 adapter API。"""
    client = AttemptObservableClientAdapter(
        SimClient(turns=[]),
        provider="sim",
        default_model="sim-model",
    )

    client.record_cache_read(123)


@pytest.mark.anyio
async def test_non_owner_exit_cannot_close_running_owner_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 owner exit 须在状态变更前拒绝，owner 最终 exactly-once cleanup。"""
    events: list[str] = []
    dispatch_started = anyio.Event()
    dispatch_allowed = anyio.Event()

    async def blocking_stream(
        inner_session: _DispatchSpySession,
        request: ApiRequest,
    ) -> AsyncGenerator[ResponseEvent, None]:
        """在真实 dispatch 窗口保持 async generator 运行中。"""
        inner_session.dispatched = True
        inner_session.requests.append(request)
        inner_session.events.append("dispatch")
        dispatch_started.set()
        await dispatch_allowed.wait()
        yield text_delta("ok-0")
        yield completed(response_id=None, usage=TokenUsage())

    monkeypatch.setattr(_DispatchSpySession, "stream", blocking_stream)
    reviewed, inner = _reviewed_spy_inner(monkeypatch, events)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver(events)
    observer.allow.set()
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    non_owner_errors: list[RuntimeError] = []
    exit_calls_during_race: list[int] = []
    state_during_race: list[tuple[bool, bool, bool]] = []

    async def exit_from_non_owner() -> None:
        """在 inner generator 正运行时从另一 task 请求退出。"""
        await dispatch_started.wait()
        try:
            await session.__aexit__(None, None, None)
        except RuntimeError as error:
            non_owner_errors.append(error)
        finally:
            exit_calls_during_race.append(inner.sessions[0].exit_calls)
            state_during_race.append((
                session._closed,  # type: ignore[attr-defined]  # noqa: SLF001
                session._active_stream is not None,  # type: ignore[attr-defined]  # noqa: SLF001
                session._active_context is not None,  # type: ignore[attr-defined]  # noqa: SLF001
            ))
            dispatch_allowed.set()

    async with session as entered:
        closer = asyncio.create_task(exit_from_non_owner())
        stream = cast(
            "AsyncGenerator[ResponseEvent, None]",
            entered.stream(_request()),
        )
        event = await anext(stream)
        await closer

    await stream.aclose()
    assert len(non_owner_errors) == 1
    assert str(non_owner_errors[0]) == "observed session exit must run in owner task"
    assert "asynchronous generator is already running" not in str(non_owner_errors[0])
    assert exit_calls_during_race == [0]
    assert state_during_race == [(False, True, True)]
    assert event.data["text"] == "ok-0"
    assert len(observer.requests) == 1
    assert events == ["ack", "session_construct", "enter", "dispatch", "exit"]
    assert inner.sessions[0].exit_calls == 1


@pytest.mark.anyio
async def test_observed_session_rejects_stream_from_non_owner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨 task 消费须在 observer/inner 前失败，outer cleanup 不覆盖终态。"""
    events: list[str] = []
    reviewed, inner = _reviewed_spy_inner(monkeypatch, events)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver(events)
    observer.allow.set()
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )

    async with session as entered:
        async def consume_one() -> ResponseEvent:
            """在独立 task 中驱动 async iterator 的第一次消费。"""
            return await anext(entered.stream(_request()))

        task = asyncio.create_task(consume_one())
        with pytest.raises(RuntimeError, match="owner task") as caught:
            await task

    assert str(caught.value) == "observed session stream must run in owner task"
    await session.__aexit__(None, None, None)
    assert observer.requests == []
    assert inner.sessions == []
    assert events == []


@pytest.mark.anyio
async def test_outer_exit_closes_inner_after_early_break_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """consumer 提前 break 后，外层 context 退出必须立即释放 inner。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [], event_count=2)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=_BlockingObserver([]),
    )
    observer = session._observer  # type: ignore[attr-defined]  # noqa: SLF001
    observer.allow.set()

    async with session as entered:
        async for _ in entered.stream(_request()):
            break

    assert inner.sessions[0].exit_calls == 1


@pytest.mark.anyio
async def test_outer_exit_closes_inner_after_consumer_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """consumer 循环体异常不能把 active inner 留给 GC finalizer。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [], event_count=2)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver([])
    observer.allow.set()
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with session as entered:
            async for _ in entered.stream(_request()):
                raise RuntimeError("consumer failed")

    assert inner.sessions[0].exit_calls == 1


@pytest.mark.anyio
async def test_outer_exit_closes_inner_when_consumer_is_cancelled_after_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首事件后的 caller cancel 仍须同步关闭 inner context。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [], event_count=2)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver([])
    observer.allow.set()
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )
    received = anyio.Event()

    async def consume_then_wait() -> None:
        async with session as entered:
            async for _ in entered.stream(_request()):
                received.set()
                await anyio.sleep_forever()

    task = asyncio.create_task(consume_then_wait())
    await received.wait()
    task.cancel("cancel after first event")
    with pytest.raises(asyncio.CancelledError, match="cancel after first event"):
        await task

    assert inner.sessions[0].exit_calls == 1


@pytest.mark.anyio
async def test_observed_session_rejects_concurrent_and_repeated_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个 observed session 只允许启动一次 stream，避免资源所有权歧义。"""
    reviewed, inner = _reviewed_spy_inner(monkeypatch, [], event_count=2)
    client = AttemptObservableClientAdapter(
        reviewed,
        provider="provider-a",
        default_model="model-a",
    )
    observer = _BlockingObserver([])
    observer.allow.set()
    session = client.session_with_attempt_observer(
        cancel=CancellationToken(name="turn"),
        attempt_observer=observer,
    )

    async with session as entered:
        first = entered.stream(_request())
        await anext(first)
        second = entered.stream(_request())
        with pytest.raises(RuntimeError, match="stream already started"):
            await anext(second)
        await first.aclose()
        repeated = entered.stream(_request())
        with pytest.raises(RuntimeError, match="stream already started"):
            await anext(repeated)

    assert inner.sessions[0].exit_calls == 1
