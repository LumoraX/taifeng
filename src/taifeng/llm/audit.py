"""LLM network attempt 的 provider-neutral 可观测边界。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import anyio

from taifeng.conversation.journal.canonical import validate_json_value
from taifeng.conversation.journal.errors import NonCanonicalValueError

if TYPE_CHECKING:
    from taifeng.llm.client import ModelClientSession, OneNetworkAttemptModelClient
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


def _freeze_json(value: object) -> object:
    """复制并冻结 observer 可见的 canonical JSON 树。"""
    if isinstance(value, dict):
        return MappingProxyType({
            key: _freeze_json(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> Any:
    """为 Journal payload 重建普通 JSON 容器。"""
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ModelAttemptRequest:
    """一次真实网络 dispatch 前交给 observer 的不可变请求快照。"""

    provider: str
    model: str
    api_request: Mapping[str, object]

    def __post_init__(self) -> None:
        """拒绝不稳定名称，并冻结完整 canonical API request。"""
        if not self.provider:
            raise ValueError("attempt provider must be non-empty")
        if not self.model:
            raise ValueError("attempt model must be non-empty")
        normalized = validate_json_value(self.api_request)
        if not isinstance(normalized, dict):
            raise NonCanonicalValueError("attempt request must be a dict")
        object.__setattr__(
            self,
            "api_request",
            _freeze_json(normalized),
        )

    def api_request_dict(self) -> dict[str, Any]:
        """返回供 durable DTO 使用的独立普通 JSON 副本。"""
        value = _thaw_json(self.api_request)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class ModelAttemptPermit:
    """observer 已取得 definite durable ack 后签发的 attempt permit。"""

    operation_id: str
    attempt_id: str
    request_record_id: str
    retry_ordinal: int

    def __post_init__(self) -> None:
        """拒绝无法稳定关联 Journal record 的 permit。"""
        if not self.operation_id:
            raise ValueError("attempt operation_id must be non-empty")
        expected_attempt = f"{self.operation_id}:attempt:{self.retry_ordinal}"
        if self.retry_ordinal < 0 or self.attempt_id != expected_attempt:
            raise ValueError("attempt identity is invalid")
        if not self.request_record_id:
            raise ValueError("attempt request_record_id must be non-empty")


class ModelAttemptObserver(Protocol):
    """每个真实网络 attempt 的 durable 前置门禁。"""

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        """durable commit request intent，并仅在 definite ack 后返回 permit。"""
        ...


class AttemptObservableModelClient(ABC):
    """attempt-aware client 的公开 API，不代表 strict audit 能力证明。

    strict gate 仅接受仓库官方 exact ``AttemptObservableClientAdapter``；
    nominal、virtual 或外部 subclass 均不能自行取得该能力。
    """

    @abstractmethod
    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """创建不带 audit observer 的 legacy session。"""

    @abstractmethod
    def session_with_attempt_observer(
        self,
        *,
        cancel: CancellationToken,
        attempt_observer: ModelAttemptObserver,
        model: str | None = None,
    ) -> ModelClientSession:
        """创建显式绑定 attempt observer 的 session。"""


class _ObservedOneAttemptSession:
    """把一次底层 stream dispatch 放到 durable observer permit 之后。"""

    def __init__(
        self,
        inner: OneNetworkAttemptModelClient,
        *,
        cancel: CancellationToken,
        observer: ModelAttemptObserver,
        provider: str,
        default_model: str,
        session_model: str | None,
    ) -> None:
        self._inner = inner
        self._cancel = cancel
        self._observer = observer
        self._provider = provider
        self._default_model = default_model
        self._session_model = session_model
        self._stream_started = False
        self._closed = False
        self._owner_task_id: int | None = None
        self._active_context: ModelClientSession | None = None
        self._active_stream: AsyncIterator[ResponseEvent] | None = None

    async def __aenter__(self) -> _ObservedOneAttemptSession:
        if self._closed:
            raise RuntimeError("observed session is closed")
        current_task_id = anyio.get_current_task().id
        if (
            self._owner_task_id is not None
            and current_task_id != self._owner_task_id
        ):
            raise RuntimeError("observed session context already has an owner task")
        self._owner_task_id = current_task_id
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._require_owner_task("exit")
        self._closed = True
        await self._close_active(*exc)

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """取得 definite permit 后才迭代底层 one-attempt stream。"""
        self._require_owner_task("stream")
        if self._closed:
            raise RuntimeError("observed session is closed")
        if self._stream_started:
            raise RuntimeError("observed session stream already started")
        self._stream_started = True
        self._cancel.raise_if_cancelled()
        model = request.model or self._session_model or self._default_model
        dispatched_request = request.model_copy(update={"model": model})
        attempt_request = ModelAttemptRequest(
            provider=self._provider,
            model=model,
            api_request=dispatched_request.model_dump(mode="json"),
        )
        permit = await self._observer.before_attempt(attempt_request)
        if type(permit) is not ModelAttemptPermit:
            raise TypeError("attempt observer returned no definite permit")
        self._cancel.raise_if_cancelled()
        inner_context = self._inner.session(
            cancel=self._cancel,
            model=self._session_model,
        )
        session = await inner_context.__aenter__()
        active_stream = session.stream(dispatched_request).__aiter__()
        self._active_context = inner_context
        self._active_stream = active_stream
        try:
            async for event in active_stream:
                yield event
        except BaseException as error:
            await self._close_active(type(error), error, error.__traceback__)
            raise
        else:
            await self._close_active(None, None, None)

    def _require_owner_task(self, action: str) -> None:
        """在任何状态或资源变更前拒绝非 owner task 操作。"""
        if anyio.get_current_task().id != self._owner_task_id:
            raise RuntimeError(
                f"observed session {action} must run in owner task"
            )

    async def _close_active(self, *exc: object) -> None:
        """摘除并 exactly-once 关闭当前 inner stream/context。"""
        active_stream = self._active_stream
        inner_context = self._active_context
        self._active_stream = None
        self._active_context = None
        if active_stream is None or inner_context is None:
            return
        try:
            close_stream = getattr(active_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()
        finally:
            await inner_context.__aexit__(*exc)


class AttemptObservableClientAdapter(AttemptObservableModelClient):
    """为当前一次 ``stream`` 等于一次网络 attempt 的 client 增加 observer。"""

    def __init__(
        self,
        inner: OneNetworkAttemptModelClient,
        *,
        provider: str,
        default_model: str,
    ) -> None:
        """冻结 provider/default model，避免请求 identity 依赖运行期猜测。"""
        if not provider:
            raise ValueError("provider must be non-empty")
        if not default_model:
            raise ValueError("default model must be non-empty")
        if type(inner) not in _reviewed_one_attempt_client_types():
            raise TypeError(
                "inner client lacks reviewed one-attempt / "
                "one-network-attempt conformance"
            )
        self._inner = inner
        self._provider = provider
        self._default_model = default_model

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """legacy 调用原样委派，不注入 observer 或改变请求。"""
        return self._inner.session(cancel=cancel, model=model)

    def record_cache_read(self, value: int) -> None:
        """把跨轮 cache read telemetry 安全转发给支持它的 inner。"""
        recorder = getattr(self._inner, "record_cache_read", None)
        if callable(recorder):
            recorder(value)

    def session_with_attempt_observer(
        self,
        *,
        cancel: CancellationToken,
        attempt_observer: ModelAttemptObserver,
        model: str | None = None,
    ) -> ModelClientSession:
        """包装一个明确只有一次真实网络 attempt 的底层 session。"""
        return _ObservedOneAttemptSession(
            self._inner,
            cancel=cancel,
            observer=attempt_observer,
            provider=self._provider,
            default_model=self._default_model,
            session_model=model,
        )


def _reviewed_one_attempt_client_types() -> tuple[type[object], ...]:
    """返回仓库逐一审查过的 exact one-attempt client 类型。"""
    from taifeng.llm.providers.anthropic_provider import AnthropicClient
    from taifeng.llm.providers.deepseek_provider import DeepSeekClient
    from taifeng.llm.providers.gemini_provider import GeminiClient
    from taifeng.llm.providers.openai_compat import OpenAICompatClient
    from taifeng.llm.providers.sim import RoutingSimClient, SimClient

    return (
        AnthropicClient,
        DeepSeekClient,
        GeminiClient,
        OpenAICompatClient,
        RoutingSimClient,
        SimClient,
    )


__all__ = [
    "AttemptObservableClientAdapter",
    "AttemptObservableModelClient",
    "ModelAttemptObserver",
    "ModelAttemptPermit",
    "ModelAttemptRequest",
]
