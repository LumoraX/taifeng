"""OpenAI 官方 ``/v1/responses`` 客户端与 terminal accumulator。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from taifeng.llm.client import ModelCapabilities, ModelClient, OneNetworkAttemptModelClient
from taifeng.llm.errors import (
    ContentFilterError,
    InvalidHistoryError,
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
    reasoning_delta,
    server_model,
    structured_output,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers._shared import (
    classify_http_error,
    classify_responses_stream_failure,
    extract_rate_limit_snapshot,
    extract_request_id,
    extract_usage_openai_family,
    iter_lines_with_cancel,
    parse_sse_data,
)
from taifeng.llm.providers.openai._shared import (
    OPENAI_DEFAULT_BASE_URL,
    build_openai_headers,
    enforce_openai_wire_size,
    tool_output_content,
)
from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
    NormalizedOutputItem,
    NormalizedReasoningItem,
    NormalizedRefusalItem,
)
from taifeng.llm.types import (
    ApiFunctionCallItem,
    ApiFunctionCallOutputItem,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    ImagePart,
    ProviderStateEnvelope,
    TextPart,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.cancellation import CancellationToken


def _input_content(
    item: ApiMessageItem,
) -> list[dict[str, Any]] | str:
    """把 message content 映射为 Responses input/output content parts。"""
    if isinstance(item.content, str):
        part_type = "output_text" if item.role == "assistant" else "input_text"
        return [{"type": part_type, "text": item.content}]
    content: list[dict[str, Any]] = []
    for part in item.content:
        if isinstance(part, TextPart):
            part_type = "output_text" if item.role == "assistant" else "input_text"
            content.append({"type": part_type, "text": part.text})
        elif isinstance(part, ImagePart):
            if item.role != "user":
                raise InvalidHistoryError("Responses images are only valid in user messages")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.media_type};base64,{part.base64_data}",
                    "detail": part.detail,
                }
            )
    return content


def _provider_state_payload(item: ApiProviderStateItem) -> dict[str, Any]:
    """校验并白名单化 OpenAI Responses reasoning state。"""
    state = item.state
    if (state.provider, state.protocol, state.item_type) != (
        "openai",
        "responses",
        "reasoning",
    ):
        raise InvalidHistoryError("foreign provider state cannot be replayed by Responses")
    allowed = {"id", "type", "encrypted_content", "summary", "status"}
    if set(state.payload) - allowed or state.payload.get("type") != "reasoning":
        raise InvalidHistoryError("invalid OpenAI reasoning provider state")
    if not state.payload.get("id") or not state.payload.get("encrypted_content"):
        raise InvalidHistoryError("incomplete OpenAI reasoning provider state")
    return dict(state.payload)


def _input_item(item: object) -> dict[str, Any]:
    """把单个 provider-neutral ordered item 映射到 Responses wire。"""
    if isinstance(item, ApiMessageItem):
        return {"type": "message", "role": item.role, "content": _input_content(item)}
    if isinstance(item, ApiFunctionCallItem):
        return {
            "type": "function_call",
            "call_id": item.call_id,
            "name": item.name,
            "arguments": item.arguments,
        }
    if isinstance(item, ApiFunctionCallOutputItem):
        return {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": tool_output_content(item.output),
        }
    if isinstance(item, ApiProviderStateItem):
        return _provider_state_payload(item)
    raise InvalidHistoryError(f"unsupported Responses input item: {type(item).__name__}")


class ResponsesAttemptAccumulator:
    """单次网络 attempt 的 preview 与 terminal 一致性缓冲。"""

    def __init__(self) -> None:
        self._text: dict[int, str] = {}
        self._arguments: dict[int, str] = {}
        self._reasoning: dict[int, str] = {}
        self._added: dict[int, dict[str, Any]] = {}
        self._emitted_calls: set[str] = set()

    def preview(self, event: dict[str, Any]) -> list[ResponseEvent]:
        """吸收一个非 terminal SSE event，并返回公开 preview events。"""
        kind = event.get("type")
        index = int(event.get("output_index", 0))
        if kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict):
                self._added[index] = item
            return []
        if kind == "response.output_text.delta":
            delta = str(event.get("delta", ""))
            self._text[index] = self._text.get(index, "") + delta
            return [text_delta(delta)] if delta else []
        if kind == "response.reasoning_summary_text.delta":
            delta = str(event.get("delta", ""))
            self._reasoning[index] = self._reasoning.get(index, "") + delta
            return [reasoning_delta(delta)] if delta else []
        if kind == "response.function_call_arguments.delta":
            delta = str(event.get("delta", ""))
            self._arguments[index] = self._arguments.get(index, "") + delta
            added = self._added.get(index, {})
            return [
                tool_call_delta(
                    call_id=str(added.get("call_id", "")),
                    name=str(added.get("name")) if added.get("name") else None,
                    delta=delta,
                )
            ] if delta else []
        if kind in {
            "response.function_call_arguments.done",
            "response.output_item.done",
        }:
            return self._preview_tool_done(event, index)
        return []

    def _preview_tool_done(
        self, event: dict[str, Any], index: int
    ) -> list[ResponseEvent]:
        """arguments/item done 任一路径最多发布一次 tool_call_done。"""
        raw = event.get("item") if isinstance(event.get("item"), dict) else {}
        added = {**self._added.get(index, {}), **raw}
        call_id = str(added.get("call_id", ""))
        name = str(added.get("name", ""))
        arguments = str(event.get("arguments", added.get("arguments", "")))
        if not call_id or not name or call_id in self._emitted_calls:
            return []
        self._emitted_calls.add(call_id)
        return [tool_call_done(call_id, name, arguments)]

    def finalize(self, response: dict[str, Any]) -> list[NormalizedOutputItem]:
        """以 terminal output 为真相，校验所有已见 preview bytes。"""
        raw_output = response.get("output")
        if not isinstance(raw_output, list) or not raw_output:
            raise InvalidResponseError("Responses terminal output is missing")
        normalized: list[NormalizedOutputItem] = []
        seen_indices: set[int] = set()
        observed_indices: list[int] = []
        for position, raw in enumerate(raw_output):
            if not isinstance(raw, dict):
                raise InvalidResponseError("Responses output item must be an object")
            index = raw.get("output_index", position)
            if isinstance(index, bool) or not isinstance(index, int) or index in seen_indices:
                raise InvalidResponseError("Responses output indexes must be unique integers")
            seen_indices.add(index)
            observed_indices.append(index)
            normalized.append(self._normalize_item(index, raw))
        if observed_indices != sorted(observed_indices):
            raise InvalidResponseError("Responses output indexes are not ordered")
        return normalized

    def _normalize_item(
        self, index: int, raw: dict[str, Any]
    ) -> NormalizedOutputItem:
        """白名单投影单个 terminal item，并逐字节核对 preview。"""
        kind = raw.get("type")
        if kind == "reasoning":
            summary = "".join(
                str(part.get("text", ""))
                for part in raw.get("summary", [])
                if isinstance(part, dict)
            )
            self._match_preview(self._reasoning, index, summary, "reasoning")
            state = None
            if raw.get("encrypted_content"):
                payload = {
                    key: raw[key]
                    for key in ("id", "type", "encrypted_content", "summary", "status")
                    if key in raw
                }
                state = ProviderStateEnvelope(
                    provider="openai",
                    protocol="responses",
                    item_type="reasoning",
                    payload=payload,
                )
            return NormalizedReasoningItem(
                output_index=index, visible_text=summary, state=state
            )
        if kind == "message":
            return self._normalize_message(index, raw)
        if kind == "function_call":
            arguments = str(raw.get("arguments", ""))
            self._match_preview(self._arguments, index, arguments, "function arguments")
            call_id = str(raw.get("call_id", ""))
            name = str(raw.get("name", ""))
            if not call_id or not name:
                raise InvalidResponseError(
                    "Responses function call identity must be non-empty"
                )
            return NormalizedFunctionCallItem(
                output_index=index,
                call_id=call_id,
                name=name,
                arguments=arguments,
            )
        raise InvalidResponseError(f"unsupported Responses output item: {kind}")

    def _normalize_message(
        self, index: int, raw: dict[str, Any]
    ) -> NormalizedOutputItem:
        """投影 assistant message；refusal 优先进入错误分支。"""
        texts: list[str] = []
        refusals: list[str] = []
        for part in raw.get("content", []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "refusal":
                refusals.append(str(part.get("refusal", "")))
        if refusals:
            return NormalizedRefusalItem(output_index=index, text="".join(refusals))
        text = "".join(texts)
        self._match_preview(self._text, index, text, "output text")
        return NormalizedMessageItem(output_index=index, text=text)

    @staticmethod
    def _match_preview(
        observed: dict[int, str], index: int, terminal: str, label: str
    ) -> None:
        """完全未收到 delta 时允许直接采用 terminal；否则必须逐字节一致。"""
        if index in observed and observed[index] != terminal:
            raise InvalidResponseError(f"{label} delta does not match terminal output")

    def missing_tool_done_events(
        self, items: list[NormalizedOutputItem]
    ) -> list[ResponseEvent]:
        """为没有 done preview 的 terminal calls 补唯一 tool_call_done。"""
        events: list[ResponseEvent] = []
        for item in items:
            if not isinstance(item, NormalizedFunctionCallItem):
                continue
            if item.call_id in self._emitted_calls:
                continue
            self._emitted_calls.add(item.call_id)
            events.append(tool_call_done(item.call_id, item.name, item.arguments))
        return events


class OpenAIResponsesSession:
    """单次 OpenAI Responses 网络 attempt。"""

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

    async def __aenter__(self) -> OpenAIResponsesSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """httpx client 在 stream scope 内关闭，无额外资源。"""

    def _build_payload(self, request: ApiRequest) -> dict[str, Any]:
        """从 canonical input_items 构造 Responses payload。

        system_prompt 以 ``role="developer"`` 下发（**不是** ``system``）：Responses
        协议下 developer 是 system 的后继角色,而部分端点会直接拒 system
        （实测 ``{"message": "System messages are not allowed"}`` HTTP 400,同端点
        developer 与顶层 instructions 均 200）。仍逐条作 input item 而非合并进顶层
        ``instructions``:多条 system_prompt 的独立性与顺序得以保留,也与 chat 协议
        侧的逐条 message 结构对称（Codex client 走 instructions 是其协议要求,见
        ``codex/wire.py``,两者刻意不同构）。
        """
        input_items = [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": prompt}],
            }
            for prompt in request.system_prompt
            if prompt
        ]
        input_items.extend(_input_item(item) for item in request.input_items)
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "input": input_items,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
                for tool in request.tools
            ]
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.response_format is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.response_format.name,
                    "schema": request.response_format.json_schema,
                    "strict": request.response_format.strict,
                }
            }
        if request.reasoning_effort is not None:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        enforce_openai_wire_size(payload, request)
        return payload

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """调用 `/responses`，只在 terminal 校验成功后发布 normalized output。"""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise InvalidRequestError("httpx required") from exc
        payload = self._build_payload(request)
        accumulator = ResponsesAttemptAccumulator()
        request_id: str | None = None
        terminal_seen = False
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
                        event = parse_sse_data(line)
                        if event is None:
                            continue
                        kind = event.get("type")
                        if kind == "response.completed":
                            if terminal_seen:
                                raise InvalidResponseError(
                                    "Responses emitted duplicate response.completed"
                                )
                            async for emitted in self._complete(
                                request, accumulator, event, request_id
                            ):
                                yield emitted
                            terminal_seen = True
                        elif kind in {"response.failed", "response.incomplete", "error"}:
                            # 与 codex provider 同源归一（ADR 0033）：官方字段 → typed
                            # LLMError，retryable / failure_class 反映真实原因。
                            err = classify_responses_stream_failure(event)
                            err.request_id = request_id
                            yield error(
                                message=str(err), kind=err.kind, retryable=err.retryable
                            )
                            raise err
                        else:
                            for emitted in accumulator.preview(event):
                                yield emitted
            except httpx.TimeoutException as exc:
                raise TransientNetworkError(f"timeout: {exc}") from exc
            except httpx.TransportError as exc:
                raise TransientNetworkError(f"transport: {exc}") from exc
        if not terminal_seen:
            raise InvalidResponseError("Responses stream ended without response.completed")

    async def _complete(
        self,
        request: ApiRequest,
        accumulator: ResponsesAttemptAccumulator,
        event: dict[str, Any],
        request_id: str | None,
    ) -> AsyncIterator[ResponseEvent]:
        """finalize terminal response，并保持 normalized_output 在 completed 之前。"""
        response = event.get("response")
        if not isinstance(response, dict):
            raise InvalidResponseError("response.completed is missing response")
        try:
            items = accumulator.finalize(response)
            refusal = next(
                (item for item in items if isinstance(item, NormalizedRefusalItem)),
                None,
            )
            if refusal is not None:
                raise ContentFilterError(refusal.text or "response refused")
        except (InvalidResponseError, ContentFilterError) as exc:
            yield error(message=str(exc), kind=exc.kind, retryable=exc.retryable)
            raise
        for emitted in accumulator.missing_tool_done_events(items):
            yield emitted
        durable_items = [
            item.model_dump(mode="json")
            for item in items
            if not isinstance(item, NormalizedRefusalItem)
        ]
        yield normalized_output(durable_items)
        terminal_text = "".join(
            item.text for item in items if isinstance(item, NormalizedMessageItem)
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
        usage = extract_usage_openai_family(response.get("usage") or {})
        yield prompt_cache(
            cache_read=usage.cache_read_input_tokens,
            cache_creation=usage.cache_creation_input_tokens,
            previous_cache_read=self._previous_cache_read,
        )
        has_calls = any(isinstance(item, NormalizedFunctionCallItem) for item in items)
        yield completed(
            response_id=str(response.get("id")) if response.get("id") else None,
            usage=usage,
            end_turn=not has_calls,
            request_id=request_id,
        )


class OpenAIResponsesClient(OneNetworkAttemptModelClient, ModelClient):
    """OpenAI 官方 Responses 客户端；手工重放 Taifeng durable history。"""

    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="openai",
        protocol="responses",
        accepts_provider_state=True,
        # Responses 的 function_call_output 原生接受 input_image content item
        tool_output_modalities=frozenset({"text", "image"}),
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.6",
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> OpenAIResponsesSession:
        """创建一个无 IO 的 Responses session。"""
        return OpenAIResponsesSession(
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
