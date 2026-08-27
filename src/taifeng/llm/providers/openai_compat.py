"""OpenAI-compat 原生 provider —— 直接走 httpx SSE，无需 LiteLLM。

适用：
    - 本地 vLLM / Ollama / one-api / new-api gateway
    - 想避开 LiteLLM 依赖的场景
    - 需要原生 cache_control / cache_breakpoints 头部控制

兼容：OpenAI v1 chat/completions API (含 stream / tool_calls)。

参照：claw-code crates/api/src/client.rs::stream
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from taifeng.llm.client import ModelCapabilities, ModelClient, OneNetworkAttemptModelClient
from taifeng.llm.errors import (
    ContentFilterError,
    InvalidRequestError,
    TransientNetworkError,
)
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    error,
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
    assert_text_only_request,
    classify_http_error,
    extract_rate_limit_snapshot,
    extract_request_id,
    extract_usage_openai_family,
)
from taifeng.llm.types import ApiRequest, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.cancellation import CancellationToken


class OpenAICompatSession:
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
        self._api_key = api_key
        self._model = model
        self._cancel = cancel
        # 仅在 api_key 非空白时附 Authorization 头。空 key（本地 Ollama / LM Studio
        # / vLLM 等无需鉴权的 OpenAI 兼容端点）应**省略**该头，而不是发出非法的
        # "Bearer "（带尾空格，会触发 httpx LocalProtocolError: Illegal header value）。
        # 对真实需鉴权的服务端：不带头 → 干净 401 → 分类为清晰的 AuthenticationError，
        # 而非把 LocalProtocolError 泄漏成 failure_class=unknown。
        self._headers = {"Content-Type": "application/json"}
        if api_key.strip():
            self._headers["Authorization"] = f"Bearer {api_key}"
        # extra_headers 在最后合并：允许网关注入自定义鉴权头（即便 api_key 为空）。
        if extra_headers:
            self._headers.update(extra_headers)
        self._timeout = timeout_seconds
        self._previous_cache_read = previous_cache_read
        self._last_usage: TokenUsage | None = None
        # 本次流最后一个非空 finish_reason —— 用于流末判定异常终止（content_filter 等）。
        self._last_finish_reason: str | None = None

    async def __aenter__(self) -> OpenAICompatSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    def _build_payload(self, req: ApiRequest) -> dict[str, Any]:
        assert_text_only_request(req)
        # system prompt 合并为一条 system 消息
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

        payload: dict[str, Any] = {
            "model": req.model or self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_output_tokens is not None:
            payload["max_tokens"] = req.max_output_tokens
        if req.tools:
            payload["tools"] = [
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
            payload["parallel_tool_calls"] = req.parallel_tool_calls
        if req.reasoning_effort:
            payload["reasoning_effort"] = req.reasoning_effort
        # P1 structured_output：req.response_format 非 None 时翻译到 OpenAI 原生格式
        if req.response_format is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.response_format.name,
                    "schema": req.response_format.json_schema,
                    "strict": req.response_format.strict,
                },
            }
        return payload

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:  # noqa: C901
        try:
            import httpx  # 必装依赖
        except ImportError as e:  # pragma: no cover
            raise InvalidRequestError("httpx required") from e

        payload = self._build_payload(request)
        url = f"{self._base_url}/chat/completions"

        yield created()
        yield server_model(payload["model"])

        tool_calls_acc: dict[int, dict[str, Any]] = {}
        # P1 structured_output：累积全文用于流末解析
        full_text_parts: list[str] = []
        # G3：服务端 request-id（成功 → completed 回流；失败 → 回填到 error）
        request_id: str | None = None

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream(
                    "POST", url, headers=self._headers, json=payload,
                ) as resp:
                    request_id = extract_request_id(resp.headers)
                    if resp.status_code != 200:
                        body = await resp.aread()
                        classified = classify_http_error(
                            resp.status_code,
                            body.decode("utf-8", errors="replace"),
                        )
                        classified.request_id = request_id
                        yield error(
                            message=str(classified),
                            kind=classified.kind,
                            retryable=classified.retryable,
                        )
                        raise classified
                    # G3：成功时若带 rate-limit 头 → emit 结构化窗口快照
                    snapshot = extract_rate_limit_snapshot(resp.headers)
                    if snapshot is not None:
                        yield rate_limits(snapshot)
                    async for line in resp.aiter_lines():
                        self._cancel.raise_if_cancelled()
                        if not line:
                            continue
                        if line.startswith(":"):
                            continue  # SSE comment
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        async for ev in self._process_chunk(chunk, tool_calls_acc):
                            if ev.kind == "text_delta":
                                full_text_parts.append(ev.data.get("text", ""))
                            yield ev
            except httpx.TimeoutException as e:
                raise TransientNetworkError(f"timeout: {e}") from e
            except httpx.TransportError as e:
                # 传输层失败统一归瞬时网络错（可重试 / 可挂起恢复）。涵盖 ``NetworkError``
                # （连接/读写/关闭）**与 ``ProtocolError``**——尤其 ``RemoteProtocolError``
                # （“Server disconnected without sending a response”，代理/网关流中途断连，
                # 本仓库实测高发）。此前只 catch ``NetworkError``，而 RemoteProtocolError 属
                # ``ProtocolError``（≠ NetworkError），会裸逃 → classify_failure 落到 unknown
                # 硬失败；归到 TransientNetworkError 后 kind=transient_network → retry_async
                # 退避重试命中，且 retryable=True 触发 turn SYSTEM_RETRY 挂起恢复。
                raise TransientNetworkError(f"transport: {e}") from e

        # finish_reason 异常终止保护：content_filter 表示响应被模型/网关安全策略主动拦截，
        # 返回空 content + 0 token。旧实现完全丢弃 finish_reason，把「被拦截」伪造成
        # 「成功的空回复」（silent fallback，违反 R 线 + 「LLM 侧不存在空回复，有即异常」不变量）。
        # 这里在流末把它显式暴露为既有分类 ContentFilterError：先 emit error 事件（与 HTTP 错误
        # 路径一致），再抛异常让上层 turn 判失败、call_skill 回 is_error=true。
        if self._last_finish_reason == "content_filter" and not tool_calls_acc:
            err = ContentFilterError(
                "response blocked by content filter (finish_reason=content_filter)"
            )
            err.request_id = request_id
            yield error(message=str(err), kind=err.kind, retryable=err.retryable)
            raise err

        # tool_call_done events
        for acc in tool_calls_acc.values():
            if acc.get("id") and acc.get("name"):
                yield tool_call_done(
                    call_id=acc["id"],
                    name=acc["name"],
                    arguments=acc.get("arguments", ""),
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
            request_id=request_id,
        )

    async def _process_chunk(
        self,
        chunk: dict[str, Any],
        tool_calls_acc: dict[int, dict[str, Any]],
    ) -> AsyncIterator[ResponseEvent]:
        choices = chunk.get("choices") or []
        if choices:
            # 记录非空 finish_reason（stop / tool_calls / content_filter / length …），
            # 供流末判定异常终止；空串/None 不覆盖已记录值。
            fr = choices[0].get("finish_reason")
            if fr:
                self._last_finish_reason = fr
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if text:
                yield text_delta(text)
            rc = delta.get("reasoning_content")
            if rc:
                yield reasoning_delta(rc)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_calls_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
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
        usage_raw = chunk.get("usage")
        if usage_raw:
            self._last_usage = extract_usage_openai_family(usage_raw)
            yield prompt_cache(
                cache_read=self._last_usage.cache_read_input_tokens,
                cache_creation=self._last_usage.cache_creation_input_tokens,
                previous_cache_read=self._previous_cache_read,
            )


class OpenAICompatClient(OneNetworkAttemptModelClient, ModelClient):
    """OpenAI-compat 原生客户端（不依赖 LiteLLM）。

    适用 vLLM / Ollama / new-api / Together / Groq / DeepSeek 等。
    """

    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text"}),
        provider="openai-compatible",
        protocol="chat",
    )

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_model = model
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> OpenAICompatSession:
        return OpenAICompatSession(
            base_url=self._base_url,
            api_key=self._api_key,
            model=model or self._default_model,
            cancel=cancel,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            previous_cache_read=self._previous_cache_read,
        )

    def record_cache_read(self, value: int) -> None:
        self._previous_cache_read = value
