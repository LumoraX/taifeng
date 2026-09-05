"""独立 Codex ``codex-responses-v1`` 网络客户端。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from taifeng.llm.client import ModelCapabilities, ModelClient, OneNetworkAttemptModelClient
from taifeng.llm.errors import (
    ContentFilterError,
    InvalidRequestError,
    InvalidResponseError,
    TransientNetworkError,
)
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    error,
    normalized_output,
    prompt_cache,
    rate_limits,
    server_model,
    structured_output,
)
from taifeng.llm.providers._shared import (
    classify_http_error,
    extract_rate_limit_snapshot,
    extract_request_id,
    iter_lines_with_cancel,
)
from taifeng.llm.providers.codex.accumulator import (
    CodexResponsesAccumulator,
    CodexTerminal,
    NoiseLedger,
)
from taifeng.llm.providers.codex.wire import build_codex_payload
from taifeng.llm.providers.openai._shared import build_openai_headers
from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


def _parse_codex_sse_line(line: str, noise: NoiseLedger) -> dict[str, Any] | None:
    """解析 Codex data 行；注释 / event 标签 / [DONE] / 非协议噪声均跳过。

    空 data 行、非 JSON、非 object 曾经硬失败——但这三种恰好是中转网关注入心跳最
    常见的形状（``data:`` 空帧、``data: ping``、``data: []``），据此终止整条流会把
    链路噪声升格成不可恢复故障（ADR 0030）。改为记账后跳过；真正的终态保证由
    ``CodexResponsesAccumulator.finalize()`` 守，噪声吞不掉输出事实。

    Args:
        line: 一行原始 SSE 文本（已去行尾换行）。
        noise: 与 accumulator 共用的噪声账；跳过的非协议帧记在这里（warn 一次）。

    Returns:
        协议 data object；``None`` 表示该行应跳过。
    """
    if not line or line.startswith(":") or line.startswith("event:"):
        return None
    if not line.startswith("data:"):
        return None
    payload = line[5:].lstrip()
    if payload == "[DONE]":
        return None
    if not payload:
        noise.record("empty-data")
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        noise.record("non-json-data")
        return None
    if not isinstance(parsed, dict):
        noise.record("non-object-data")
        return None
    return parsed


class CodexResponsesSession:
    """一次 Codex Responses HTTP attempt。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        cancel: CancellationToken,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        previous_cache_read: int = 0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._cancel = cancel
        self._headers = build_openai_headers(api_key, extra_headers)
        self._timeout = timeout_seconds
        self._previous_cache_read = previous_cache_read

    async def __aenter__(self) -> CodexResponsesSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """HTTP client 生命周期完全位于 stream scope。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """执行一次 `/responses` 请求，并把 terminal 延迟到 clean EOF。"""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise InvalidRequestError("httpx required") from exc
        payload = build_codex_payload(request, default_model=self._model)
        accumulator = CodexResponsesAccumulator()
        request_id: str | None = None
        yield created()
        yield server_model(payload["model"])
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/responses",
                    headers=self._headers,
                    json=payload,
                ) as response:
                    request_id = extract_request_id(response.headers)
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        classified = classify_http_error(response.status_code, body)
                        classified.request_id = request_id
                        yield error(
                            message=str(classified),
                            kind=classified.kind,
                            retryable=classified.retryable,
                        )
                        raise classified
                    snapshot = extract_rate_limit_snapshot(response.headers)
                    if snapshot is not None:
                        yield rate_limits(snapshot)
                    async for line in iter_lines_with_cancel(response, self._cancel):
                        event = _parse_codex_sse_line(line, accumulator.noise)
                        if event is None:
                            continue
                        try:
                            previews = accumulator.accept(event)
                        except (InvalidResponseError, ContentFilterError) as exc:
                            yield error(
                                message=str(exc),
                                kind=exc.kind,
                                retryable=exc.retryable,
                            )
                            raise
                        for preview in previews:
                            yield preview
            except httpx.TimeoutException as exc:
                raise TransientNetworkError(f"timeout: {exc}") from exc
            except httpx.TransportError as exc:
                raise TransientNetworkError(f"transport: {exc}") from exc
        try:
            terminal = accumulator.finalize()
        except InvalidResponseError as exc:
            yield error(message=str(exc), kind=exc.kind, retryable=exc.retryable)
            raise
        async for terminal_event in self._terminal_events(
            request,
            terminal,
            request_id,
        ):
            yield terminal_event

    async def _terminal_events(
        self,
        request: ApiRequest,
        terminal: CodexTerminal,
        request_id: str | None,
    ) -> AsyncIterator[ResponseEvent]:
        """按 normalized_output → structured/cache → completed 发布终态。"""
        durable_items = [item.model_dump(mode="json") for item in terminal.items]
        yield normalized_output(durable_items)
        terminal_text = "".join(
            item.text
            for item in terminal.items
            if isinstance(item, NormalizedMessageItem)
        )
        if request.response_format is not None:
            try:
                parsed = json.loads(terminal_text)
            except json.JSONDecodeError as exc:
                yield error(
                    message=f"structured_output_parse_failed: {exc}",
                    kind="parse_error",
                    retryable=False,
                )
            else:
                yield structured_output(parsed=parsed, raw_text=terminal_text)
        yield prompt_cache(
            cache_read=terminal.usage.cache_read_input_tokens,
            cache_creation=terminal.usage.cache_creation_input_tokens,
            previous_cache_read=self._previous_cache_read,
        )
        has_calls = any(
            isinstance(item, NormalizedFunctionCallItem) for item in terminal.items
        )
        yield completed(
            response_id=terminal.response_id,
            usage=terminal.usage,
            end_turn=not has_calls,
            request_id=request_id,
        )


class CodexResponsesClient(OneNetworkAttemptModelClient, ModelClient):
    """Codex 代理专用 Responses client；不提供 Chat fallback。"""

    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="codex",
        protocol="responses",
        accepts_provider_state=True,
        # Responses 的 function_call_output 原生接受 input_image content item
        tool_output_modalities=frozenset({"text", "image"}),
    )

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "gpt-5.6-luna",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> CodexResponsesSession:
        """创建无 IO 的 Codex one-attempt session。"""
        return CodexResponsesSession(
            base_url=self._base_url,
            api_key=self._api_key,
            model=model or self._model,
            cancel=cancel,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            previous_cache_read=self._previous_cache_read,
        )

    def record_cache_read(self, value: int) -> None:
        """保存上一轮 cache read，供 cache-break 事件比较。"""
        self._previous_cache_read = value


__all__ = ["CodexResponsesClient", "CodexResponsesSession"]
