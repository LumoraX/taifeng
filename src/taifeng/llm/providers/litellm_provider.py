"""LiteLLM provider 适配 —— 统一接入 OpenAI / Anthropic / Gemini / 本地模型。

参照：claw-code crates/api/src/providers/* 多 provider 适配模式
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from taifeng.llm.client import ModelClient
from taifeng.llm.errors import (
    AuthenticationError,
    ContentFilterError,
    ContextOverflowError,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    error,
    prompt_cache,
    server_model,
    structured_output,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken


def _classify_litellm_error(exc: Exception) -> LLMError:
    """把 LiteLLM 的异常分类到 Taifeng 的错误层级。

    优先级：rate_limit > context_overflow > content_filter > auth >
    transient_network > server_error > invalid_request（兜底）。

    判断同时看类名（``type(exc).__name__``）与 message 内容 —— LiteLLM 常把
    上游网络 / 5xx 错误包成 ``InternalServerError`` 子类，类名不带 connection /
    timeout 关键字，但 message 含 ``"Connection error"`` / ``"5xx"`` 等线索。
    """
    message = str(exc)
    msg_lower = message.lower()
    cls_name = type(exc).__name__.lower()

    if "ratelimit" in cls_name or "rate_limit" in msg_lower or "rate limit" in msg_lower:
        retry_after = getattr(exc, "retry_after", None)
        return RateLimitError(message, retry_after_seconds=retry_after)
    if "context" in msg_lower and ("length" in msg_lower or "overflow" in msg_lower):
        return ContextOverflowError(message)
    if "content_filter" in msg_lower or "safety" in msg_lower:
        return ContentFilterError(message)
    if "auth" in cls_name or "401" in message or "403" in message:
        return AuthenticationError(message)
    # 网络层：类名 或 message 任一出现 timeout / connection 关键字
    # LiteLLM 的 "InternalServerError - Connection error." 即走这条
    if (
        "timeout" in cls_name
        or "connection" in cls_name
        or "timeout" in msg_lower
        or "connection error" in msg_lower
        or "connection reset" in msg_lower
        or "connection refused" in msg_lower
        or "remote disconnected" in msg_lower
    ):
        return TransientNetworkError(message)
    # 服务端 5xx：HTTP 状态码出现在 message 或类名为 InternalServerError
    if (
        any(code in message for code in ("500", "502", "503", "504"))
        or "internalservererror" in cls_name
        or "serviceunavailable" in cls_name
        or "badgateway" in cls_name
        or "gatewaytimeout" in cls_name
    ):
        return ServerError(message)
    return InvalidRequestError(message)


def _to_litellm_messages(req: ApiRequest) -> list[dict[str, Any]]:
    """将 Taifeng ApiRequest 转 LiteLLM messages 格式。"""
    messages: list[dict[str, Any]] = []
    for sp in req.system_prompt:
        if sp:
            messages.append({"role": "system", "content": sp})
    for m in req.messages:
        msg: dict[str, Any] = {"role": m.role}
        if isinstance(m.content, str):
            msg["content"] = m.content
        else:
            msg["content"] = m.content
        if m.tool_call_id is not None:
            msg["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        # reasoning-content-passback:thinking 模型续传契约,None 时不写键
        if m.reasoning is not None:
            msg["reasoning_content"] = m.reasoning
        messages.append(msg)
    return messages


def _to_litellm_tools(req: ApiRequest) -> list[dict[str, Any]] | None:
    if not req.tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in req.tools
    ]


def _extract_usage(raw: dict[str, Any]) -> TokenUsage:
    """从 LiteLLM 的 usage payload 提取 Taifeng TokenUsage。"""
    pt = raw.get("prompt_tokens", 0)
    ct = raw.get("completion_tokens", 0)
    tt = raw.get("total_tokens", pt + ct)
    cache_creation = raw.get("cache_creation_input_tokens", 0)
    cache_read = raw.get("cache_read_input_tokens", 0)
    # OpenAI prompt_tokens_details.cached_tokens
    pt_details = raw.get("prompt_tokens_details") or {}
    if not cache_read and isinstance(pt_details, dict):
        cache_read = pt_details.get("cached_tokens", 0)
    ct_details = raw.get("completion_tokens_details") or {}
    reasoning = 0
    if isinstance(ct_details, dict):
        reasoning = ct_details.get("reasoning_tokens", 0)
    return TokenUsage(
        input_tokens=pt,
        output_tokens=ct,
        total_tokens=tt,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        reasoning_tokens=reasoning,
        raw=raw,
    )


class LiteLLMSession:
    """单 turn 的 LiteLLM 调用 session。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str | None,
        extra_params: dict[str, Any],
        cancel: CancellationToken,
        previous_cache_read: int = 0,
        force_httpx_transport: bool = True,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._extra_params = extra_params
        self._cancel = cancel
        self._previous_cache_read = previous_cache_read
        self._force_httpx_transport = force_httpx_transport
        self._last_usage: TokenUsage | None = None

    async def __aenter__(self) -> LiteLLMSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:  # noqa: C901
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise InvalidRequestError(
                "LiteLLM not installed. Run: pip install 'taifeng[litellm]'"
            ) from e

        # 抑制 LiteLLM 默认日志
        litellm.suppress_debug_info = True

        # 强制走 httpx transport，绕开 LiteLLM 1.7x 默认的 aiohttp transport：
        # aiohttp 经 HTTPS_PROXY 做 TLS-in-TLS 时 SNI 处理有已知 bug
        # （aiohttp 在 _create_proxy_connection 路径下可能向上游发送错误的 SNI，
        # 触发服务端 ``TLSV1_UNRECOGNIZED_NAME`` 告警），在国内本地代理
        # （Clash / V2Ray / 7890 端口）+ 第三方 OpenAI-compat endpoint 场景必现。
        # httpx 是 LiteLLM 之前的稳定默认，在 TLS-in-TLS 下行为正确。
        # 注意：此处会污染进程级 litellm 模块状态；如业务侧同进程混用 litellm
        # 且需要 aiohttp，请显式构造 ``LiteLLMClient(force_httpx_transport=False)``。
        if self._force_httpx_transport:
            litellm.disable_aiohttp_transport = True

        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": _to_litellm_messages(request),
            "stream": True,
            **self._extra_params,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["api_base"] = self._base_url
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        tools = _to_litellm_tools(request)
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        if request.reasoning_effort:
            kwargs["reasoning_effort"] = request.reasoning_effort
        # P1 structured_output：LiteLLM 已统一 response_format 字段，
        # 内部对 Anthropic / Gemini / OpenAI 自动桥接
        if request.response_format is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_format.name,
                    "schema": request.response_format.json_schema,
                    "strict": request.response_format.strict,
                },
            }

        # stream_options 以让 OpenAI 兼容路径返回 usage
        kwargs.setdefault("stream_options", {"include_usage": True})

        yield created()
        yield server_model(kwargs["model"])

        # 累积 tool call 增量
        tool_calls_acc: dict[str, dict[str, Any]] = {}
        # P1 structured_output：累积全文用于流末解析
        full_text_parts: list[str] = []

        try:
            response = await litellm.acompletion(**kwargs)
            async for chunk in response:
                self._cancel.raise_if_cancelled()
                chunk_dict = chunk if isinstance(chunk, dict) else chunk.model_dump()
                choices = chunk_dict.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    # 文本增量
                    text = delta.get("content")
                    if text:
                        full_text_parts.append(text)
                        yield text_delta(text)
                    # 推理增量（Claude thinking / OpenAI o1）
                    reasoning_content = delta.get("reasoning_content")
                    if reasoning_content:
                        from taifeng.llm.events import reasoning_delta
                        yield reasoning_delta(reasoning_content)
                    # Tool calls 增量
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        key = f"tc:{idx}"
                        acc = tool_calls_acc.setdefault(
                            key,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
                            yield tool_call_delta(
                                call_id=acc["id"],
                                name=acc["name"] or None,
                                delta=fn["arguments"],
                            )
                # Usage 统计（最后一个 chunk 通常携带）
                usage_raw = chunk_dict.get("usage")
                if usage_raw:
                    self._last_usage = _extract_usage(usage_raw)
                    # 发 prompt_cache 事件
                    yield prompt_cache(
                        cache_read=self._last_usage.cache_read_input_tokens,
                        cache_creation=self._last_usage.cache_creation_input_tokens,
                        previous_cache_read=self._previous_cache_read,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            classified = _classify_litellm_error(exc)
            yield error(message=str(classified), kind=classified.kind, retryable=classified.retryable)
            raise classified from exc

        # tool_call_done 事件
        for acc in tool_calls_acc.values():
            if acc["id"] and acc["name"]:
                yield tool_call_done(
                    call_id=acc["id"],
                    name=acc["name"],
                    arguments=acc["arguments"],
                )

        # P1 structured_output：流末若请求带 response_format → 尝试 json 解析
        if request.response_format is not None:
            full_text = "".join(full_text_parts)
            try:
                parsed = json.loads(full_text)
            except json.JSONDecodeError as exc:
                yield error(
                    message=f"structured_output_parse_failed: {exc}",
                    kind="parse_error",
                    retryable=False,
                )
            else:
                yield structured_output(parsed=parsed, raw_text=full_text)

        yield completed(
            response_id=None,
            usage=self._last_usage or TokenUsage(),
            end_turn=not bool(tool_calls_acc),
        )

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._last_usage


class LiteLLMClient(ModelClient):
    """Session 级 LiteLLM 客户端。

    Args:
        force_httpx_transport: 默认 ``True`` —— 在每次 ``stream`` 调用前把
            ``litellm.disable_aiohttp_transport`` 置 True，强制 LiteLLM 走 httpx
            而非 aiohttp transport。规避 aiohttp 在 HTTPS_PROXY + TLS-in-TLS
            场景下 SNI 错误导致的 ``TLSV1_UNRECOGNIZED_NAME`` 失败（国内本地
            代理 + 第三方 OpenAI-compat endpoint 场景必现）。如业务侧同进程
            混用 litellm 且确需 aiohttp，传 ``False``。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
        extra_params: dict[str, Any] | None = None,
        force_httpx_transport: bool = True,
    ) -> None:
        self._api_key = api_key
        self._default_model = model
        self._base_url = base_url
        self._extra_params = extra_params or {}
        self._previous_cache_read = 0
        self._force_httpx_transport = force_httpx_transport

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> LiteLLMSession:
        return LiteLLMSession(
            api_key=self._api_key,
            model=model or self._default_model,
            base_url=self._base_url,
            extra_params=self._extra_params,
            cancel=cancel,
            previous_cache_read=self._previous_cache_read,
            force_httpx_transport=self._force_httpx_transport,
        )

    def record_cache_read(self, value: int) -> None:
        """记录最近一次 cache_read，用于下一轮的 break 检测。"""
        self._previous_cache_read = value
