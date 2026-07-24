"""LLM network attempt 的 provider-neutral 可观测边界。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from taifeng.llm.client import ModelClientSession, OneNetworkAttemptModelClient
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


def _freeze_json(value: object) -> object:
    """复制并冻结 observer 可见的 canonical JSON 树。"""
    if isinstance(value, dict):
        return MappingProxyType({
            str(key): _freeze_json(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> Any:
    """为 Journal payload 重建普通 JSON 容器。"""
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_json(item)
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
        object.__setattr__(
            self,
            "api_request",
            _freeze_json(dict(self.api_request)),
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
    """audit 模式要求 nominal 实现的 attempt-aware client 边界。"""

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

    async def __aenter__(self) -> _ObservedOneAttemptSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """取得 definite permit 后才迭代底层 one-attempt stream。"""
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
        async with self._inner.session(
            cancel=self._cancel,
            model=self._session_model,
        ) as session:
            async for event in session.stream(dispatched_request):
                yield event


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
        from taifeng.llm.client import OneNetworkAttemptModelClient

        inner_type = type(inner)
        mro = type.__getattribute__(inner_type, "__mro__")
        if OneNetworkAttemptModelClient not in mro:
            raise TypeError("inner client lacks nominal one-network-attempt capability")
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


__all__ = [
    "AttemptObservableClientAdapter",
    "AttemptObservableModelClient",
    "ModelAttemptObserver",
    "ModelAttemptPermit",
    "ModelAttemptRequest",
]
