"""LLM network attempt 的 provider-neutral 可观测边界。"""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import anyio

from taifeng.conversation.journal.canonical import validate_json_value
from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.conversation.journal.records import (
    LlmStatus,
    StableErrorV1,
    stable_error,
)
from taifeng.llm.client import ModelClientSession, model_capabilities
from taifeng.llm.image_input import redact_sensitive_request_data

if TYPE_CHECKING:
    from taifeng.llm.client import OneNetworkAttemptModelClient
    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


def _freeze_json(value: object) -> object:
    """复制并冻结 observer 可见的 canonical JSON 树。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> Any:
    """为 Journal payload 重建普通 JSON 容器。"""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
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


@dataclass(frozen=True, slots=True)
class ModelAttemptOutcome:
    """adapter 在 provider attempt 终止后交给 observer 的稳定快照。"""

    permit: ModelAttemptPermit
    status: LlmStatus
    normalized_items: tuple[Mapping[str, object], ...]
    usage: Mapping[str, object] | None
    provider_request_id: str | None
    stable_error: StableErrorV1 | None

    def __post_init__(self) -> None:
        """冻结 canonical items/usage，并校验 status/error 配对。"""
        items = validate_json_value(_thaw_json(self.normalized_items))
        usage = validate_json_value(_thaw_json(self.usage)) if self.usage is not None else None
        if not isinstance(items, list):
            raise NonCanonicalValueError("normalized items must be a list")
        if usage is not None and not isinstance(usage, dict):
            raise NonCanonicalValueError("attempt usage must be a dict")
        if self.status is LlmStatus.COMPLETE:
            if self.stable_error is not None:
                raise ValueError("complete attempt forbids stable_error")
        elif self.stable_error is None:
            raise ValueError("non-complete attempt requires stable_error")
        object.__setattr__(self, "normalized_items", _freeze_json(items))
        object.__setattr__(
            self,
            "usage",
            _freeze_json(usage) if usage is not None else None,
        )

    def normalized_items_list(self) -> list[Any]:
        """返回供 durable DTO 使用的普通 canonical items 副本。"""
        value = _thaw_json(self.normalized_items)
        assert isinstance(value, list)
        return value

    def usage_dict(self) -> dict[str, Any] | None:
        """返回供 durable DTO 使用的普通 usage 副本。"""
        value = _thaw_json(self.usage)
        assert value is None or isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class ModelAttemptCheckpoint:
    """observer definite durable ack 后返回的 attempt checkpoint lineage。"""

    operation_id: str
    attempt_id: str
    request_record_id: str
    checkpoint_record_id: str
    retry_ordinal: int
    status: LlmStatus
    normalized_items: tuple[Mapping[str, object], ...]
    usage: Mapping[str, object] | None
    provider_request_id: str | None
    stable_error: StableErrorV1 | None

    def __post_init__(self) -> None:
        """复用 outcome 校验，并拒绝不稳定 checkpoint identity。"""
        permit = ModelAttemptPermit(
            operation_id=self.operation_id,
            attempt_id=self.attempt_id,
            request_record_id=self.request_record_id,
            retry_ordinal=self.retry_ordinal,
        )
        outcome = ModelAttemptOutcome(
            permit=permit,
            status=self.status,
            normalized_items=self.normalized_items,
            usage=self.usage,
            provider_request_id=self.provider_request_id,
            stable_error=self.stable_error,
        )
        if not self.checkpoint_record_id:
            raise ValueError("checkpoint_record_id must be non-empty")
        object.__setattr__(self, "normalized_items", outcome.normalized_items)
        object.__setattr__(self, "usage", outcome.usage)

    def normalized_items_list(self) -> list[Any]:
        """返回供 durable llm_response_committed DTO 使用的普通 items 副本。"""
        value = _thaw_json(self.normalized_items)
        assert isinstance(value, list)
        return value

    def usage_dict(self) -> dict[str, Any] | None:
        """返回供 durable llm_response_committed DTO 使用的普通 usage 副本。"""
        value = _thaw_json(self.usage)
        assert value is None or isinstance(value, dict)
        return value


class ModelAttemptObserver(Protocol):
    """每个真实网络 attempt 的 durable request/checkpoint 门禁。"""

    @property
    def finalization_timeout(self) -> float:
        """返回 cancellation-independent finalization 的注入期限。"""
        ...

    async def before_attempt(
        self,
        request: ModelAttemptRequest,
    ) -> ModelAttemptPermit:
        """durable commit request intent，并仅在 definite ack 后返回 permit。"""
        ...

    async def after_attempt(
        self,
        outcome: ModelAttemptOutcome,
    ) -> ModelAttemptCheckpoint:
        """durable commit attempt checkpoint，并返回 definite ack lineage。"""
        ...


class AttemptResultUnknownError(RuntimeError):
    """provider stream 未形成可信终态，禁止发布缓冲事件。"""


class _ProviderErrorEventError(RuntimeError):
    """provider 以 error event 结束，异常原文不得进入 durable payload。"""


class _SilentAttemptEndError(RuntimeError):
    """provider stream 静默结束，attempt 结果不确定。"""


def _event_text(event: ResponseEvent, key: str) -> str:
    """只提取 provider event 中已归一化的字符串字段。"""
    value = event.data.get(key)
    return value if isinstance(value, str) else ""


def _normalized_items(
    events: list[ResponseEvent],
) -> tuple[Mapping[str, object], ...]:
    """按首次出现顺序把 provider delta 归约为完整 response items。"""
    terminal = [event for event in events if event.kind == "normalized_output"]
    if terminal:
        if len(terminal) != 1:
            raise ValueError("attempt emitted duplicate normalized output")
        normalized = validate_json_value(terminal[0].data.get("items"))
        if not isinstance(normalized, list) or not all(
            isinstance(item, dict) for item in normalized
        ):
            raise NonCanonicalValueError("normalized output must contain JSON objects")
        return tuple(normalized)
    items: list[dict[str, object]] = []
    positions: dict[tuple[str, str], int] = {}
    for event in events:
        key: tuple[str, str] | None = None
        item: dict[str, object] | None = None
        delta_key = ""
        if event.kind == "reasoning_delta":
            key = ("reasoning", "")
            item = {"kind": "reasoning", "text": ""}
            delta_key = "delta"
        elif event.kind == "text_delta":
            key = ("assistant", "")
            item = {"kind": "assistant", "text": ""}
            delta_key = "text"
        elif event.kind in {"tool_call_delta", "tool_call_done"}:
            call_id = _event_text(event, "call_id")
            key = ("function_call", call_id)
            item = {
                "kind": "function_call",
                "call_id": call_id,
                "name": _event_text(event, "name"),
                "arguments": "",
            }
        if key is None or item is None:
            continue
        if key not in positions:
            positions[key] = len(items)
            items.append(item)
        current = items[positions[key]]
        if delta_key:
            current["text"] = str(current["text"]) + _event_text(
                event,
                delta_key,
            )
        elif event.kind == "tool_call_delta":
            current["name"] = _event_text(event, "name") or current["name"]
            current["arguments"] = str(current["arguments"]) + _event_text(
                event,
                "delta",
            )
        else:
            current["name"] = _event_text(event, "name")
            current["arguments"] = _event_text(event, "arguments")
    return tuple(items)


def _completed_metadata(
    events: list[ResponseEvent],
) -> tuple[Mapping[str, object] | None, str | None]:
    """提取 terminal completed 的 usage/provider request id。"""
    if not events or events[-1].kind != "completed":
        return None, None
    data = events[-1].data
    usage = data.get("usage")
    request_id = data.get("request_id") or data.get("response_id")
    return (
        usage if isinstance(usage, dict) else None,
        request_id if isinstance(request_id, str) and request_id else None,
    )


def _attempt_outcome(
    permit: ModelAttemptPermit,
    events: list[ResponseEvent],
    error: BaseException | None,
) -> ModelAttemptOutcome:
    """把 complete/error/cancel/silent-end 归一化为 observer 输入。"""
    usage, provider_request_id = _completed_metadata(events)
    status = LlmStatus.COMPLETE
    failure: StableErrorV1 | None = None
    if error is not None:
        status = (
            LlmStatus.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            or getattr(error, "failure_class", None) == "cancelled"
            else LlmStatus.ERROR
        )
        failure = stable_error(error)
        error_request_id = getattr(error, "request_id", None)
        if isinstance(error_request_id, str) and error_request_id:
            provider_request_id = error_request_id
    elif not events or events[-1].kind not in {"completed", "error"}:
        status = LlmStatus.UNKNOWN
        failure = stable_error(_SilentAttemptEndError())
    elif events[-1].kind == "error":
        status = LlmStatus.ERROR
        failure = stable_error(_ProviderErrorEventError())
    return ModelAttemptOutcome(
        permit=permit,
        status=status,
        normalized_items=_normalized_items(events),
        usage=usage,
        provider_request_id=provider_request_id,
        stable_error=failure,
    )


def _safe_attempt_outcome(
    permit: ModelAttemptPermit,
    events: list[ResponseEvent],
    error: BaseException | None,
) -> ModelAttemptOutcome:
    """runtime normalization 失败时降为不含任意值的 UNKNOWN outcome。"""
    try:
        return _attempt_outcome(permit, events, error)
    except BaseException as outcome_error:
        try:
            items = _normalized_items(events)
        except BaseException:
            items = ()
        return ModelAttemptOutcome(
            permit=permit,
            status=LlmStatus.UNKNOWN,
            normalized_items=items,
            usage=None,
            provider_request_id=None,
            stable_error=stable_error(outcome_error),
        )


async def _run_after_attempt(
    observer: ModelAttemptObserver,
    outcome: ModelAttemptOutcome,
) -> ModelAttemptCheckpoint:
    """在 observer 注入的期限内 shield checkpoint finalization。"""
    timeout = observer.finalization_timeout
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("attempt finalization_timeout must be positive")
    with anyio.fail_after(float(timeout), shield=True):
        return await observer.after_attempt(outcome)


async def _await_checkpoint_owned(
    observer: ModelAttemptObserver,
    outcome: ModelAttemptOutcome,
) -> tuple[ModelAttemptCheckpoint, asyncio.CancelledError | None]:
    """独立 task 收敛 checkpoint；重复 raw cancel 只延迟到 ack 后传播。

    参照 anyio 优先原则，差异：此处必须直接用 asyncio。anyio 未暴露
    ``Task.cancelling()`` / ``uncancel()``，而「取消到达也要先落 durable ack
    再传播」正依赖这两个原语区分外部取消与 worker 自身异常；因此这条 finalization
    路径硬绑定 asyncio 后端（anyio-on-trio 不适用）。

    取消传播优先级（M-2 记录）：仅当 worker 正常返回 checkpoint 时，才把首个
    延迟到的 ``CancelledError`` 作为第二元素回传给调用点补抛；若 worker 自身抛出
    （UNKNOWN freeze / finalization 超时），则以 worker 异常为准、丢弃已 uncancel
    的取消——因为此时会话已进入 freeze/错误终态，取消语义被更强的失败覆盖。
    """
    worker = asyncio.create_task(
        _run_after_attempt(observer, outcome),
        name=f"llm-checkpoint:{outcome.permit.attempt_id}",
    )
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is None or current.cancelling() == 0:
                raise
            cancellation = cancellation or error
            current.uncancel()
    return worker.result(), cancellation


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


class AttemptObservedModelClientSession(ModelClientSession, Protocol):
    """strict observed session 暴露最后 definite checkpoint lineage。"""

    @property
    def last_attempt_checkpoint(self) -> ModelAttemptCheckpoint | None:
        """checkpoint ack 前为 None；ack 后返回不可变 lineage/result。"""
        ...


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
        """冻结 observer/provider/model 与初始 owner-task 无主的会话状态。"""
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
        self._last_attempt_checkpoint: ModelAttemptCheckpoint | None = None

    @property
    def last_attempt_checkpoint(self) -> ModelAttemptCheckpoint | None:
        """返回最后 definite durable checkpoint，供后续 logical commit 读取。"""
        return self._last_attempt_checkpoint

    async def __aenter__(self) -> _ObservedOneAttemptSession:
        """绑定唯一 owner task：进入后仅该 task 可 stream / 关闭本会话。"""
        if self._closed:
            raise RuntimeError("observed session is closed")
        current_task_id = anyio.get_current_task().id
        if self._owner_task_id is not None and current_task_id != self._owner_task_id:
            raise RuntimeError("observed session context already has an owner task")
        self._owner_task_id = current_task_id
        return self

    async def __aexit__(self, *exc: object) -> None:
        """由 owner task 幂等关闭：标记关闭并 exactly-once 摘除 inner 资源。"""
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
            api_request=redact_sensitive_request_data(
                dispatched_request.model_dump(mode="json")
            ),
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
        events: list[ResponseEvent] = []
        try:
            async for event in active_stream:
                events.append(event)
        except BaseException as error:
            await self._close_active(type(error), error, error.__traceback__)
            outcome = _safe_attempt_outcome(permit, events, error)
            checkpoint, _ = await _await_checkpoint_owned(
                self._observer,
                outcome,
            )
            self._accept_checkpoint(outcome, checkpoint)
            raise
        else:
            await self._close_active(None, None, None)
            outcome = _safe_attempt_outcome(permit, events, None)
            checkpoint, cancellation = await _await_checkpoint_owned(
                self._observer,
                outcome,
            )
            self._accept_checkpoint(outcome, checkpoint)
            if cancellation is not None:
                raise cancellation
            if outcome.status is not LlmStatus.COMPLETE:
                raise AttemptResultUnknownError(
                    "observed provider attempt has no complete terminal"
                )
            for event in events:
                yield event

    def _accept_checkpoint(
        self,
        outcome: ModelAttemptOutcome,
        checkpoint: ModelAttemptCheckpoint,
    ) -> None:
        """只接受与 permit/outcome exact 一致的 definite checkpoint ack。"""
        if type(checkpoint) is not ModelAttemptCheckpoint:
            raise TypeError("attempt observer returned no definite checkpoint")
        expected = (
            outcome.permit.operation_id,
            outcome.permit.attempt_id,
            outcome.permit.request_record_id,
            outcome.permit.retry_ordinal,
            outcome.status,
            outcome.normalized_items,
            outcome.usage,
            outcome.provider_request_id,
            outcome.stable_error,
        )
        actual = (
            checkpoint.operation_id,
            checkpoint.attempt_id,
            checkpoint.request_record_id,
            checkpoint.retry_ordinal,
            checkpoint.status,
            checkpoint.normalized_items,
            checkpoint.usage,
            checkpoint.provider_request_id,
            checkpoint.stable_error,
        )
        if actual != expected:
            raise ValueError("attempt checkpoint does not match outcome")
        self._last_attempt_checkpoint = checkpoint

    def _require_owner_task(self, action: str) -> None:
        """在任何状态或资源变更前拒绝非 owner task 操作。"""
        if anyio.get_current_task().id != self._owner_task_id:
            raise RuntimeError(f"observed session {action} must run in owner task")

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
                "inner client lacks reviewed one-attempt / one-network-attempt conformance"
            )
        self._inner = inner
        self._provider = provider
        self._default_model = default_model
        self.capabilities = model_capabilities(inner)

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
    ) -> AttemptObservedModelClientSession:
        """包装一个明确只有一次真实网络 attempt 的底层 session。"""
        timeout = getattr(attempt_observer, "finalization_timeout", None)
        if (
            not callable(getattr(attempt_observer, "before_attempt", None))
            or not callable(getattr(attempt_observer, "after_attempt", None))
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise TypeError("attempt observer lacks bounded checkpoint protocol")
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
    from taifeng.llm.providers.openai.chat import OpenAIChatClient
    from taifeng.llm.providers.openai.responses import OpenAIResponsesClient
    from taifeng.llm.providers.openai_compat import OpenAICompatClient
    from taifeng.llm.providers.sim import RoutingSimClient, SimClient

    return (
        AnthropicClient,
        DeepSeekClient,
        GeminiClient,
        OpenAICompatClient,
        OpenAIChatClient,
        OpenAIResponsesClient,
        RoutingSimClient,
        SimClient,
    )


__all__ = [
    "AttemptObservedModelClientSession",
    "AttemptResultUnknownError",
    "AttemptObservableClientAdapter",
    "AttemptObservableModelClient",
    "ModelAttemptCheckpoint",
    "ModelAttemptObserver",
    "ModelAttemptOutcome",
    "ModelAttemptPermit",
    "ModelAttemptRequest",
]
