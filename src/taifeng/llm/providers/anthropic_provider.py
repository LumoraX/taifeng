"""Anthropic 原生 provider —— 直连 messages API，零 anthropic-sdk 依赖。

适用：Anthropic Claude（含 cache_control 精准控制 / extended thinking）。

参照：
    hermes-agent/agent/anthropic_adapter.py（messages API SSE 形状）
    Anthropic 官方文档：https://docs.anthropic.com/en/api/messages-streaming

与 LiteLLM 路径的差异：
    - 错误分类基于 httpx 异常类型 + HTTP status code（不靠 message 关键字）
    - cache_creation_input_tokens / cache_read_input_tokens 从 message_start
      / message_delta 直接读，无 provider 转换层
    - tool_calls 走 Anthropic 原生 tool_use / tool_result block，不经
      OpenAI 形状中转
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from taifeng.llm.client import ModelClient
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
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers._shared import (
    classify_http_error,
    extract_rate_limit_snapshot,
    extract_request_id,
    extract_usage_anthropic,
    parse_sse_event,
)
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken

# Anthropic API 默认 anthropic-version
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
# Anthropic messages.create 必填 max_tokens，给一个稳妥兜底
_DEFAULT_MAX_TOKENS = 4096


def _to_anthropic_messages(
    req: ApiRequest,
    *,
    cache_indexes: set[int],
) -> tuple[str | None, list[dict[str, Any]]]:
    """把 ``ApiRequest`` 翻译为 Anthropic ``system`` + ``messages``。

    转换规则：
        - ``system_prompt: list[str]`` → 拼接为单 string 作为 top-level ``system``
        - role=``user`` → 保留 ``user``
        - role=``assistant`` → 保留 ``assistant``；若有 ``tool_calls`` 则翻译为
          ``content: [{type: "tool_use", id, name, input}]``（带前置 text 块）
        - role=``tool`` → 翻译为 ``user`` 角色 + ``content: [{type: "tool_result",
          tool_use_id, content}]``（Anthropic 把 tool result 当 user 输入）
        - 连续同 role 消息会按 Anthropic 要求合并 content 块
        - ``cache_indexes`` 中索引对应的消息最后一个 content block 加
          ``cache_control: {type: "ephemeral"}``
    """
    # system prompt 合并
    sys_parts = [s for s in req.system_prompt if s]
    system_str: str | None = "\n\n".join(sys_parts) if sys_parts else None

    out: list[dict[str, Any]] = []
    for idx, msg in enumerate(req.messages):
        anth_role = "user" if msg.role == "tool" else msg.role
        if anth_role == "system":
            # system 已合并到 top-level，跳过
            continue

        content_blocks: list[dict[str, Any]] = []

        # tool role → tool_result block
        if msg.role == "tool":
            tool_use_id = msg.tool_call_id or ""
            raw_content = (
                msg.content
                if isinstance(msg.content, str)
                else json.dumps(msg.content)
            )
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": raw_content,
            })
        else:
            # 文本 content
            if isinstance(msg.content, str):
                if msg.content:
                    content_blocks.append(
                        {"type": "text", "text": msg.content},
                    )
            elif isinstance(msg.content, list):
                # 已是 block list（业务侧直接传 Anthropic 形状）→ 透传
                content_blocks.extend(msg.content)

            # assistant 的 tool_calls → tool_use blocks
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
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })

        # cache_control 注入到最后一个 content block
        if idx in cache_indexes and content_blocks:
            content_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        if not content_blocks:
            continue

        # 合并连续同 role 消息
        if out and out[-1]["role"] == anth_role:
            out[-1]["content"].extend(content_blocks)
        else:
            out.append({"role": anth_role, "content": content_blocks})

    return system_str, out


def _to_anthropic_tools(req: ApiRequest) -> list[dict[str, Any]] | None:
    """``ToolSpecRef`` → Anthropic ``tools`` 字段；schema 字段是 ``input_schema``。"""
    if not req.tools:
        return None
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in req.tools
    ]


class AnthropicSession:
    """单 turn Anthropic messages API 调用 session。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        cancel: CancellationToken,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        anthropic_version: str = _DEFAULT_ANTHROPIC_VERSION,
        previous_cache_read: int = 0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._cancel = cancel
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
            **(extra_headers or {}),
        }
        self._timeout = timeout_seconds
        self._previous_cache_read = previous_cache_read
        self._last_usage: TokenUsage | None = None

    async def __aenter__(self) -> AnthropicSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    def _build_payload(self, request: ApiRequest) -> dict[str, Any]:
        cache_indexes = {bp.index for bp in request.cache_breakpoints}
        system_str, messages = _to_anthropic_messages(
            request, cache_indexes=cache_indexes,
        )
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or _DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        if system_str is not None:
            payload["system"] = system_str
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        tools = _to_anthropic_tools(request)
        if tools is not None:
            payload["tools"] = tools
        return payload

    async def stream(  # noqa: C901
        self, request: ApiRequest,
    ) -> AsyncIterator[ResponseEvent]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise InvalidRequestError(
                "httpx required for AnthropicClient",
            ) from exc

        payload = self._build_payload(request)
        url = f"{self._base_url}/v1/messages"

        yield created()
        yield server_model(payload["model"])

        # content_block index → 累积 tool_use 数据
        # key: index, value: {"id": str, "name": str, "arguments": str(json partial)}
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        # 当前活跃的 content_block index → type（text / tool_use）
        block_types: dict[int, str] = {}
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
                            provider="anthropic",
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

                    # Anthropic SSE 是 event:\ndata: 双行分组
                    event_buffer: list[str] = []
                    async for line in resp.aiter_lines():
                        self._cancel.raise_if_cancelled()
                        if line == "":
                            # 事件分隔空行 → 处理累积缓冲
                            if event_buffer:
                                async for ev in self._process_event(
                                    event_buffer, tool_calls_acc, block_types,
                                ):
                                    yield ev
                                event_buffer = []
                            continue
                        event_buffer.append(line)
                    # 末尾残留事件
                    if event_buffer:
                        async for ev in self._process_event(
                            event_buffer, tool_calls_acc, block_types,
                        ):
                            yield ev
            except httpx.TimeoutException as exc:
                raise TransientNetworkError(f"anthropic timeout: {exc}") from exc
            except httpx.NetworkError as exc:
                raise TransientNetworkError(f"anthropic network: {exc}") from exc

        # 流末 tool_call_done 事件
        for acc in tool_calls_acc.values():
            if acc.get("id") and acc.get("name"):
                yield tool_call_done(
                    call_id=acc["id"],
                    name=acc["name"],
                    arguments=acc.get("arguments", ""),
                )

        # prompt_cache + completed（end_turn 已由 message_delta 写入 _end_turn）
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

    _end_turn: bool = True  # 默认 end_turn=True；message_delta 含 stop_reason 时更新

    async def _process_event(
        self,
        lines: list[str],
        tool_calls_acc: dict[int, dict[str, Any]],
        block_types: dict[int, str],
    ) -> AsyncIterator[ResponseEvent]:
        """处理一个完整的 Anthropic SSE 事件块。"""
        name, payload = parse_sse_event(lines)
        if not name or not payload:
            return

        if name == "content_block_start":
            idx = payload.get("index", 0)
            block = payload.get("content_block") or {}
            btype = block.get("type", "text")
            block_types[idx] = btype
            if btype == "tool_use":
                tool_calls_acc[idx] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": "",
                }
            return

        if name == "content_block_delta":
            idx = payload.get("index", 0)
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                t = delta.get("text", "")
                if t:
                    yield text_delta(t)
            elif dtype == "input_json_delta":
                partial = delta.get("partial_json", "")
                acc = tool_calls_acc.get(idx)
                if acc and partial:
                    acc["arguments"] += partial
                    yield tool_call_delta(
                        call_id=acc["id"],
                        name=acc["name"] or None,
                        delta=partial,
                    )
            return

        if name == "content_block_stop":
            # Anthropic 不要求在 block_stop 发任何事件
            return

        if name == "message_delta":
            # 含 stop_reason 与 usage
            delta = payload.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason is not None:
                # tool_use → 还有 tool 要跑，end_turn=False
                self._end_turn = stop_reason in {"end_turn", "stop_sequence"}
            usage_raw = payload.get("usage")
            if usage_raw:
                # message_delta 的 usage 是增量（仅 output_tokens 等），需要合并
                # 简化处理：直接覆盖（input_tokens 已在 message_start 拿到）
                if self._last_usage is None:
                    self._last_usage = extract_usage_anthropic(usage_raw)
                else:
                    merged = dict(self._last_usage.raw)
                    merged.update(usage_raw)
                    self._last_usage = extract_usage_anthropic(merged)
            return

        if name == "message_start":
            msg = payload.get("message") or {}
            usage_raw = msg.get("usage")
            if usage_raw:
                self._last_usage = extract_usage_anthropic(usage_raw)
            return

        if name == "message_stop":
            return

        if name == "ping":
            return

        if name == "error":
            err = payload.get("error") or {}
            classified = classify_http_error(
                500,
                json.dumps(err),
                provider="anthropic",
            )
            yield error(
                message=str(classified),
                kind=classified.kind,
                retryable=classified.retryable,
            )
            raise classified

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._last_usage


class AnthropicClient(ModelClient):
    """Session 级 Anthropic native 客户端。

    构造参数：
        api_key: ANTHROPIC_API_KEY（业务侧从环境变量读后注入）
        model: 默认模型名（不带 LiteLLM 前缀），如 ``claude-haiku-4-5-20251001``
        base_url: 默认 ``https://api.anthropic.com``
        extra_headers: 额外 header（用于 third-party 网关）
        timeout_seconds: httpx 超时
        anthropic_version: API 版本（默认 ``2023-06-01``）
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        base_url: str = "https://api.anthropic.com",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
        anthropic_version: str = _DEFAULT_ANTHROPIC_VERSION,
    ) -> None:
        self._api_key = api_key
        self._default_model = model
        self._base_url = base_url
        self._extra_headers = extra_headers
        self._timeout_seconds = timeout_seconds
        self._anthropic_version = anthropic_version
        self._previous_cache_read = 0

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> AnthropicSession:
        return AnthropicSession(
            api_key=self._api_key,
            model=model or self._default_model,
            base_url=self._base_url,
            cancel=cancel,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            anthropic_version=self._anthropic_version,
            previous_cache_read=self._previous_cache_read,
        )

    def record_cache_read(self, value: int) -> None:
        self._previous_cache_read = value


__all__ = ["AnthropicClient", "AnthropicSession"]
