"""一次网络 attempt 可观测 LLM client adapter 测试。"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.llm.audit import (
    AttemptObservableClientAdapter,
    ModelAttemptPermit,
    ModelAttemptRequest,
)
from taifeng.llm.client import OneNetworkAttemptModelClient
from taifeng.llm.events import text_delta
from taifeng.llm.providers.anthropic_provider import AnthropicClient
from taifeng.llm.providers.gemini_provider import GeminiClient
from taifeng.llm.providers.litellm_provider import LiteLLMClient
from taifeng.llm.providers.openai_compat import OpenAICompatClient
from taifeng.llm.providers.sim import SimClient
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.audit_config import AttemptObservableModelClient
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.dispatched = False
        self.requests: list[ApiRequest] = []

    async def __aenter__(self) -> _DispatchSpySession:
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        self.dispatched = True
        self.requests.append(request)
        self.events.append("dispatch")
        yield text_delta("ok")


class _OneAttemptClient(OneNetworkAttemptModelClient):
    """每个 session 只进行一次真实 stream dispatch 的受控 client。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events
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
        session = _DispatchSpySession(self.events)
        self.sessions.append(session)
        return session


class _BlockingObserver:
    """显式 gate durable permit，便于证明 ack-before-dispatch。"""

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


class _FailingObserver:
    """在 durable permit 前失败。"""

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        del request
        raise RuntimeError("journal unavailable")


async def _consume(session: ModelClientSession, request: ApiRequest) -> list[str]:
    """完整消费 adapter stream。"""
    async with session as entered:
        return [
            str(event.data.get("text", ""))
            async for event in entered.stream(request)
        ]


@pytest.mark.anyio
async def test_durable_observer_ack_precedes_actual_dispatch() -> None:
    """observer barrier 未放行时底层 stream 必须完全未开始。"""
    events: list[str] = []
    inner = _OneAttemptClient(events)
    client = AttemptObservableClientAdapter(
        inner,
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

    assert result == ["ok"]
    assert events == ["ack", "session_construct", "enter", "dispatch"]
    assert inner.sessions[0].dispatched is True
    assert observer.requests[0].provider == "provider-a"
    assert observer.requests[0].model == "model-a"
    assert observer.requests[0].api_request["model"] == "model-a"
    assert inner.sessions[0].requests[0].model == "model-a"


@pytest.mark.anyio
async def test_observer_failure_prevents_dispatch() -> None:
    """没有 definite durable permit 时不得调用底层 stream。"""
    inner = _OneAttemptClient([])
    client = AttemptObservableClientAdapter(
        inner,
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
async def test_target_cancel_during_observer_barrier_prevents_dispatch() -> None:
    """target token 在 durable permit 前取消时不得触发 provider。"""
    inner = _OneAttemptClient([])
    client = AttemptObservableClientAdapter(
        inner,
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
async def test_raw_caller_cancel_during_observer_barrier_prevents_dispatch() -> None:
    """caller task 原生取消时 adapter 不吞取消且不触发 provider。"""
    inner = _OneAttemptClient([])
    client = AttemptObservableClientAdapter(
        inner,
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


def test_adapter_is_nominal_attempt_observable_client_and_legacy_is_untouched() -> None:
    """真实 adapter 通过 nominal gate，普通 session 原样委派。"""
    inner = _OneAttemptClient([])
    client = AttemptObservableClientAdapter(
        inner,
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
    [SimClient, OpenAICompatClient, AnthropicClient, GeminiClient],
)
def test_reviewed_direct_clients_nominally_declare_one_attempt(
    client_type: type[object],
) -> None:
    """只有已审查直连实现和 Sim 明确继承受控 attempt 边界。"""
    assert OneNetworkAttemptModelClient in client_type.__mro__


def test_litellm_does_not_nominally_declare_one_attempt() -> None:
    """LiteLLM opaque fallback/retry surface 不能拥有 nominal marker。"""
    assert OneNetworkAttemptModelClient not in LiteLLMClient.__mro__
