"""Gemini 原生 provider —— 直连 streamGenerateContent，零 google-genai-sdk 依赖。

适用：Google AI Studio Gemini API（含 functionCall / cachedContent 元数据）。

参照：https://ai.google.dev/api/generate-content#streamGenerateContent

与 LiteLLM 路径的差异：
    - role 映射 ``assistant`` → ``model``、``tool`` → ``function`` 在本层完成
    - tools 字段是 ``[{functionDeclarations: [...]}]`` 嵌套结构
    - usage 从 ``usageMetadata`` 直接读（含 ``cachedContentTokenCount``）
    - functionCall 不流式发 args delta：上游整体到达，本层一次性 emit
      ``tool_call_done``（不发 ``tool_call_delta``）
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, Literal

from taifeng.llm.client import ModelClient, OneNetworkAttemptModelClient
from taifeng.llm.errors import (
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
    server_model,
    text_delta,
    tool_call_done,
)
from taifeng.llm.providers._shared import (
    classify_http_error,
    extract_rate_limit_snapshot,
    extract_request_id,
    extract_usage_gemini,
    parse_sse_data,
)
from taifeng.llm.types import ApiRequest, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.cancellation import CancellationToken

# Gemini role 映射
_ROLE_MAP = {
    "user": "user",
    "assistant": "model",
    "tool": "function",
    "system": "user",  # system 已合并到 systemInstruction；保险兜底
}


def _to_gemini_contents(
    req: ApiRequest,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """把 ``ApiRequest`` 翻译为 Gemini ``systemInstruction`` + ``contents``。

    转换规则：
        - ``system_prompt: list[str]`` → 合并为 ``systemInstruction.parts[].text``
        - role 映射：assistant → model，tool → function
        - 文本 content → ``parts: [{text}]``
        - assistant.tool_calls → ``parts: [{functionCall: {name, args}}]``
        - tool 角色的 result → ``parts: [{functionResponse: {name, response}}]``
    """
    sys_parts = [s for s in req.system_prompt if s]
    system_instruction: dict[str, Any] | None = None
    if sys_parts:
        system_instruction = {
            "parts": [{"text": "\n\n".join(sys_parts)}],
        }

    contents: list[dict[str, Any]] = []
    for msg in req.messages:
        if msg.role == "system":
            continue  # 已合并到 systemInstruction

        gem_role = _ROLE_MAP.get(msg.role, "user")
        parts: list[dict[str, Any]] = []

        # tool 角色 → functionResponse
        if msg.role == "tool":
            # Gemini 要求 functionResponse 的 name 字段。ApiMessage 没存
            # function name，用 tool_call_id 兜底（业务侧若需要可直接传
            # list 形态 content 透传）
            if isinstance(msg.content, list):
                parts.extend(msg.content)
            else:
                raw = (
                    msg.content
                    if isinstance(msg.content, str)
                    else json.dumps(msg.content)
                )
                # 把 string 包成 functionResponse.response.content
                parts.append({
                    "functionResponse": {
                        "name": msg.tool_call_id or "",
                        "response": {"content": raw},
                    },
                })
        else:
            # 文本 content
            if isinstance(msg.content, str):
                if msg.content:
                    parts.append({"text": msg.content})
            elif isinstance(msg.content, list):
                parts.extend(msg.content)

            # assistant.tool_calls → functionCall
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    fn = tc.get("function") or {}
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = (
                            json.loads(args_raw)
                            if isinstance(args_raw, str)
                            else args_raw
                        )
                    except json.JSONDecodeError:
                        args = {}
                    parts.append({
                        "functionCall": {
                            "name": fn.get("name", ""),
                            "args": args,
                        },
                    })

        if not parts:
            continue
        contents.append({"role": gem_role, "parts": parts})

    return system_instruction, contents


def _to_gemini_tools(req: ApiRequest) -> list[dict[str, Any]] | None:
    """``ToolSpecRef`` → Gemini ``tools: [{functionDeclarations: [...]}]``。"""
    if not req.tools:
        return None
    return [{
        "functionDeclarations": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            for t in req.tools
        ],
    }]


class GeminiSession:
    """单 turn Gemini streamGenerateContent 调用 session。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        cancel: CancellationToken,
        auth_via: Literal["query", "header"] = "query",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        previous_cache_read: int = 0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._cancel = cancel
        self._auth_via = auth_via
        self._timeout = timeout_seconds
        self._previous_cache_read = previous_cache_read
        self._last_usage: TokenUsage | None = None
        self._end_turn = True

        # 空 key 时省略鉴权（与其余 native client 一致）。header 模式不发空
        # x-goog-api-key；query 模式见 _build_url —— 空 key 不挂 &key=。
        headers: dict[str, str] = {"content-type": "application/json"}
        if auth_via == "header" and api_key.strip():
            headers["x-goog-api-key"] = api_key
        if extra_headers:
            headers.update(extra_headers)
        self._headers = headers

    async def __aenter__(self) -> GeminiSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    def _build_payload(self, request: ApiRequest) -> dict[str, Any]:
        system_instruction, contents = _to_gemini_contents(request)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction is not None:
            payload["systemInstruction"] = system_instruction
        gen_config: dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_output_tokens
        if gen_config:
            payload["generationConfig"] = gen_config
        tools = _to_gemini_tools(request)
        if tools is not None:
            payload["tools"] = tools
        return payload

    def _build_url(self, model: str) -> str:
        url = (
            f"{self._base_url}/v1beta/models/{model}:streamGenerateContent"
            "?alt=sse"
        )
        # 空 key 不挂 &key=（避免发出语义为空的鉴权参数；真实服务端会干净 401）。
        if self._auth_via == "query" and self._api_key.strip():
            url += f"&key={self._api_key}"
        return url

    async def stream(  # noqa: C901
        self, request: ApiRequest,
    ) -> AsyncIterator[ResponseEvent]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise InvalidRequestError(
                "httpx required for GeminiClient",
            ) from exc

        model = request.model or self._model
        payload = self._build_payload(request)
        url = self._build_url(model)

        yield created()
        yield server_model(model)

        # functionCall 不流式发 args delta —— 整体到达后一次性 emit done
        pending_tool_calls: list[dict[str, Any]] = []
        request_id: str | None = None  # G3：服务端 request-id

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
                            provider="gemini",
                        )
                        classified.request_id = request_id
                        yield error(
                            message=str(classified),
                            kind=classified.kind,
                            retryable=classified.retryable,
                        )
                        raise classified

                    snapshot = extract_rate_limit_snapshot(resp.headers)
                    if snapshot is not None:
                        yield rate_limits(snapshot)

                    async for line in resp.aiter_lines():
                        self._cancel.raise_if_cancelled()
                        chunk = parse_sse_data(line)
                        if chunk is None:
                            continue
                        async for ev in self._process_chunk(
                            chunk, pending_tool_calls,
                        ):
                            yield ev
            except httpx.TimeoutException as exc:
                raise TransientNetworkError(f"gemini timeout: {exc}") from exc
            except httpx.NetworkError as exc:
                raise TransientNetworkError(f"gemini network: {exc}") from exc

        # 流末把累积的 functionCall 整体发出
        for tc in pending_tool_calls:
            yield tool_call_done(
                call_id=tc["call_id"],
                name=tc["name"],
                arguments=tc["arguments"],
            )

        if self._last_usage is not None:
            yield prompt_cache(
                cache_read=self._last_usage.cache_read_input_tokens,
                cache_creation=self._last_usage.cache_creation_input_tokens,
                previous_cache_read=self._previous_cache_read,
            )

        yield completed(
            response_id=None,
            usage=self._last_usage or TokenUsage(),
            end_turn=self._end_turn,
            request_id=request_id,
        )

    async def _process_chunk(
        self,
        chunk: dict[str, Any],
        pending_tool_calls: list[dict[str, Any]],
    ) -> AsyncIterator[ResponseEvent]:
        """处理一个 SSE chunk —— Gemini chunk 形状 {candidates, usageMetadata}。"""
        candidates = chunk.get("candidates") or []
        if candidates:
            cand = candidates[0]
            content = cand.get("content") or {}
            for part in content.get("parts") or []:
                if "text" in part:
                    t = part.get("text", "")
                    if t:
                        yield text_delta(t)
                elif "functionCall" in part:
                    fc = part["functionCall"] or {}
                    name = fc.get("name", "")
                    args = fc.get("args", {}) or {}
                    pending_tool_calls.append({
                        "call_id": f"fc_{uuid.uuid4().hex[:24]}",
                        "name": name,
                        "arguments": json.dumps(args),
                    })

            finish_reason = cand.get("finishReason")
            if finish_reason is not None:
                # STOP → end_turn=True；其他（TOOL_CALL / MAX_TOKENS / ...） → False
                self._end_turn = finish_reason == "STOP"

        # usage 元数据通常在末 chunk
        usage_raw = chunk.get("usageMetadata")
        if usage_raw:
            self._last_usage = extract_usage_gemini(usage_raw)

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._last_usage


class GeminiClient(OneNetworkAttemptModelClient, ModelClient):
    """Session 级 Gemini native 客户端。

    构造参数：
        api_key: GEMINI_API_KEY（业务侧从环境变量读后注入）
        model: 默认模型名，如 ``gemini-2.0-flash-exp``
        base_url: 默认 ``https://generativelanguage.googleapis.com``
        auth_via: ``"query"``（默认，URL 上挂 ``?key=``） / ``"header"``
        extra_headers: 额外 header
        timeout_seconds: httpx 超时
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.0-flash-exp",
        base_url: str = "https://generativelanguage.googleapis.com",
        auth_via: Literal["query", "header"] = "query",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._api_key = api_key
        self._default_model = model
        self._base_url = base_url
        self._auth_via = auth_via
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> GeminiSession:
        return GeminiSession(
            api_key=self._api_key,
            model=model or self._default_model,
            base_url=self._base_url,
            cancel=cancel,
            auth_via=self._auth_via,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            previous_cache_read=self._previous_cache_read,
        )

    def record_cache_read(self, value: int) -> None:
        self._previous_cache_read = value


__all__ = ["GeminiClient", "GeminiSession"]
