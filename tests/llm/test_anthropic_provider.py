"""`AnthropicClient` / `AnthropicSession` 单元测试。

覆盖 spec ``llm-provider-native`` 的 Requirement「AnthropicClient 走 native
messages API」全部 4 个 Scenario + 错误路径。

测试用 ``httpx.MockTransport`` 模拟上游 SSE 响应，**CI 内不触真 API**。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from taifeng.llm.errors import (
    AuthenticationError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
)
from taifeng.llm.providers.anthropic_provider import (
    AnthropicClient,
    AnthropicSession,
    _to_anthropic_messages,
    _to_anthropic_tools,
)
from taifeng.llm.types import ApiMessage, ApiRequest, CacheBreakpoint, ToolSpecRef
from taifeng.loop.cancellation import CancellationToken

# ============================================================
# helpers —— 构造 SSE 响应字符串
# ============================================================


def _sse_event(name: str, data: dict[str, Any]) -> str:
    """构造一个完整的 Anthropic SSE 事件块（event:\\ndata:\\n\\n）。"""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _build_anthropic_stream(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    """把 (event_name, data) 列表拼成 SSE 字节流。"""
    return "".join(_sse_event(n, d) for n, d in events).encode("utf-8")


async def _consume(gen: Any) -> list[Any]:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def _make_session(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str = "sk-ant-test",
    model: str = "claude-haiku-4-5-20251001",
) -> AnthropicSession:
    """构造 session 并把 httpx transport 替换为 MockTransport。"""
    session = AnthropicSession(
        api_key=api_key,
        model=model,
        base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    # monkey-patch httpx.AsyncClient 让 mock transport 生效
    return session


# ============================================================
# 1. payload 构造测试（_to_anthropic_messages / _to_anthropic_tools）
# ============================================================


def test_to_anthropic_messages_basic_text() -> None:
    req = ApiRequest(
        model="claude",
        system_prompt=["You are helpful.", "Be concise."],
        messages=[ApiMessage(role="user", content="hi")],
    )
    sys_str, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert sys_str == "You are helpful.\n\nBe concise."
    assert msgs == [{
        "role": "user",
        "content": [{"type": "text", "text": "hi"}],
    }]


def test_to_anthropic_messages_tool_use_block() -> None:
    """assistant.tool_calls → tool_use block。"""
    req = ApiRequest(
        model="claude",
        messages=[
            ApiMessage(role="user", content="search for cats"),
            ApiMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "tu_001",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"cats"}',
                    },
                }],
            ),
        ],
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert msgs[1]["role"] == "assistant"
    # 仅 tool_use（empty text 被过滤）
    blocks = msgs[1]["content"]
    assert len(blocks) == 1
    assert blocks[0] == {
        "type": "tool_use",
        "id": "tu_001",
        "name": "search",
        "input": {"q": "cats"},
    }


def test_to_anthropic_messages_tool_result_becomes_user() -> None:
    """role=tool → user role + tool_result block。"""
    req = ApiRequest(
        model="claude",
        messages=[
            ApiMessage(
                role="tool",
                content="found 42 cats",
                tool_call_id="tu_001",
            ),
        ],
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert msgs == [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "tu_001",
            "content": "found 42 cats",
        }],
    }]


def test_to_anthropic_messages_cache_control_injection() -> None:
    """cache_breakpoints 指向的消息最后一个 block 加 cache_control。"""
    req = ApiRequest(
        model="claude",
        messages=[
            ApiMessage(role="user", content="part 1"),
            ApiMessage(role="user", content="part 2"),
        ],
        cache_breakpoints=[CacheBreakpoint(index=0)],
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes={0})
    # 连续 user 被合并为一条 msg
    assert len(msgs) == 1
    blocks = msgs[0]["content"]
    # 第一个 block（来自 idx=0）应有 cache_control
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}
    # 第二个 block（来自 idx=1，无 cache）不应有
    assert "cache_control" not in blocks[1]


def test_to_anthropic_messages_merges_consecutive_same_role() -> None:
    req = ApiRequest(
        model="claude",
        messages=[
            ApiMessage(role="user", content="a"),
            ApiMessage(role="user", content="b"),
            ApiMessage(role="assistant", content="ack"),
        ],
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert len(msgs[0]["content"]) == 2  # 合并了两条
    assert msgs[1]["role"] == "assistant"


def test_to_anthropic_tools_uses_input_schema_field() -> None:
    """tools.schema 字段是 input_schema 不是 parameters。"""
    req = ApiRequest(
        model="claude",
        messages=[ApiMessage(role="user", content="hi")],
        tools=[ToolSpecRef(
            name="search",
            description="web search",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )],
    )
    tools = _to_anthropic_tools(req)
    assert tools is not None
    assert tools[0] == {
        "name": "search",
        "description": "web search",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }


# ============================================================
# 2. 端到端 SSE 流（用 httpx.MockTransport）
# ============================================================


@pytest.mark.asyncio
async def test_stream_minimal_text_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """最小文本 turn：message_start → 多个 text_delta → message_delta →
    message_stop。"""
    body = _build_anthropic_stream([
        ("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_01",
                "model": "claude-test",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 20,
                },
            },
        }),
        ("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": ", world!"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        }),
        ("message_stop", {"type": "message_stop"}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["model"] == "claude-haiku-4-5-20251001"
        return httpx.Response(200, content=body)

    _patch_httpx(monkeypatch, handler)

    session = AnthropicSession(
        api_key="sk-ant-test",
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    req = ApiRequest(
        model="claude-haiku-4-5-20251001",
        messages=[ApiMessage(role="user", content="hi")],
    )
    events = await _consume(session.stream(req))
    kinds = [e.kind for e in events]
    assert kinds[0] == "created"
    assert kinds[1] == "server_model"
    assert "text_delta" in kinds
    assert "prompt_cache" in kinds
    assert kinds[-1] == "completed"
    completed = events[-1]
    assert completed.data["end_turn"] is True
    assert completed.data["usage"]["input_tokens"] == 100
    assert completed.data["usage"]["output_tokens"] == 5
    # cache 元数据精准透传
    assert completed.data["usage"]["cache_read_input_tokens"] == 20


@pytest.mark.asyncio
async def test_stream_tool_use_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_use 流式：content_block_start(type=tool_use) → input_json_delta*
    → message_delta(stop_reason=tool_use)。"""
    body = _build_anthropic_stream([
        ("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_02",
                "model": "claude-test",
                "usage": {"input_tokens": 50, "output_tokens": 0},
            },
        }),
        ("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "tu_X",
                "name": "search",
                "input": {},
            },
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 30},
        }),
        ("message_stop", {"type": "message_stop"}),
    ])

    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, content=body))

    session = AnthropicSession(
        api_key="k",
        model="claude-test",
        base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    events = await _consume(session.stream(ApiRequest(
        model="claude-test",
        messages=[ApiMessage(role="user", content="search hi")],
    )))
    # tool_call_delta（两次 partial_json）+ 流末 tool_call_done
    delta_evs = [e for e in events if e.kind == "tool_call_delta"]
    done_evs = [e for e in events if e.kind == "tool_call_done"]
    assert len(delta_evs) == 2
    assert delta_evs[0].data["delta"] == '{"q":'
    assert delta_evs[1].data["delta"] == '"hi"}'
    assert len(done_evs) == 1
    assert done_evs[0].data["call_id"] == "tu_X"
    assert done_evs[0].data["name"] == "search"
    assert done_evs[0].data["arguments"] == '{"q":"hi"}'
    # stop_reason=tool_use → end_turn=False
    assert events[-1].data["end_turn"] is False


# ============================================================
# 3. 错误路径
# ============================================================


@pytest.mark.asyncio
async def test_401_raises_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(401, content=b'{"error":{"type":"authentication_error"}}'),
    )
    session = AnthropicSession(
        api_key="bad", model="claude", base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(AuthenticationError):
        await _consume(session.stream(ApiRequest(
            model="claude",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_429_raises_rate_limit_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(
            429,
            content=b'{"error":{"type":"rate_limit_error","retry_after":10}}',
        ),
    )
    session = AnthropicSession(
        api_key="k", model="claude", base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(RateLimitError) as ei:
        await _consume(session.stream(ApiRequest(
            model="claude",
            messages=[ApiMessage(role="user", content="hi")],
        )))
    assert ei.value.retry_after_seconds == 10.0


@pytest.mark.asyncio
async def test_500_raises_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(500, content=b"upstream exploded"),
    )
    session = AnthropicSession(
        api_key="k", model="claude", base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(ServerError):
        await _consume(session.stream(ApiRequest(
            model="claude",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_connect_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _patch_httpx(monkeypatch, handler)
    session = AnthropicSession(
        api_key="k", model="claude", base_url="https://api.anthropic.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(TransientNetworkError):
        await _consume(session.stream(ApiRequest(
            model="claude",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_cancel_midstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token 在 stream 中途取消 → raise CancelledError。"""
    body = _build_anthropic_stream([
        ("message_start", {
            "type": "message_start",
            "message": {"id": "m", "model": "c", "usage": {
                "input_tokens": 10, "output_tokens": 0,
            }},
        }),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }),
    ])
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, content=body))

    cancel = CancellationToken()
    cancel.cancel()  # 提前取消
    session = AnthropicSession(
        api_key="k", model="claude", base_url="https://api.anthropic.com",
        cancel=cancel,
    )
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await _consume(session.stream(ApiRequest(
            model="claude",
            messages=[ApiMessage(role="user", content="hi")],
        )))


# ============================================================
# 4. Client 层（ModelClient 协议契合）
# ============================================================


def test_client_default_constructor() -> None:
    c = AnthropicClient(api_key="sk-x")
    assert c._default_model == "claude-haiku-4-5-20251001"
    assert c._base_url == "https://api.anthropic.com"


def test_client_session_returns_session() -> None:
    c = AnthropicClient(api_key="sk-x")
    s = c.session(cancel=CancellationToken())
    assert isinstance(s, AnthropicSession)


def test_client_record_cache_read() -> None:
    c = AnthropicClient(api_key="sk-x")
    c.record_cache_read(100)
    assert c._previous_cache_read == 100


# ============================================================
# helpers —— monkey-patch httpx.AsyncClient.stream 让 MockTransport 生效
# ============================================================


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """把 httpx.AsyncClient 替换成挂 MockTransport 的版本。"""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
