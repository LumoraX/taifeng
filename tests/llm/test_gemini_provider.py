"""`GeminiClient` / `GeminiSession` 单元测试。

覆盖 spec ``llm-provider-native`` 的 Requirement「GeminiClient 走
streamGenerateContent SSE」全部 4 个 Scenario + 错误路径。
"""

from __future__ import annotations

import asyncio
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
from taifeng.llm.providers.gemini_provider import (
    GeminiClient,
    GeminiSession,
    _to_gemini_contents,
    _to_gemini_tools,
)
from taifeng.llm.types import ApiMessage, ApiRequest, ToolSpecRef
from taifeng.loop.cancellation import CancellationToken

# ============================================================
# helpers —— 构造 SSE 流（Gemini 单行 data:）
# ============================================================


def _sse_chunk(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _build_stream(chunks: list[dict[str, Any]]) -> bytes:
    return "".join(_sse_chunk(c) for c in chunks).encode("utf-8")


async def _consume(gen: Any) -> list[Any]:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ============================================================
# 1. payload 翻译
# ============================================================


def test_to_gemini_contents_basic_text() -> None:
    req = ApiRequest(
        model="gemini",
        system_prompt=["You are X.", "Be Y."],
        messages=[ApiMessage(role="user", content="hi")],
    )
    sys_inst, contents = _to_gemini_contents(req)
    assert sys_inst == {"parts": [{"text": "You are X.\n\nBe Y."}]}
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_to_gemini_contents_assistant_role_renamed_model() -> None:
    req = ApiRequest(
        model="gemini",
        messages=[
            ApiMessage(role="user", content="q"),
            ApiMessage(role="assistant", content="a"),
        ],
    )
    _, contents = _to_gemini_contents(req)
    assert contents[1]["role"] == "model"


def test_to_gemini_contents_function_call_part() -> None:
    """assistant.tool_calls → functionCall part。"""
    req = ApiRequest(
        model="gemini",
        messages=[
            ApiMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "fc_1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"hi"}',
                    },
                }],
            ),
        ],
    )
    _, contents = _to_gemini_contents(req)
    assert contents[0]["role"] == "model"
    parts = contents[0]["parts"]
    assert parts == [{"functionCall": {"name": "search", "args": {"q": "hi"}}}]


def test_to_gemini_contents_function_response() -> None:
    """tool role → function role + functionResponse part。"""
    req = ApiRequest(
        model="gemini",
        messages=[
            ApiMessage(
                role="tool",
                content="42 results",
                tool_call_id="search",
            ),
        ],
    )
    _, contents = _to_gemini_contents(req)
    assert contents == [{
        "role": "function",
        "parts": [{
            "functionResponse": {
                "name": "search",
                "response": {"content": "42 results"},
            },
        }],
    }]


def test_to_gemini_tools_nested_function_declarations() -> None:
    """tools 是 [{functionDeclarations: [...]}] 嵌套结构。"""
    req = ApiRequest(
        model="gemini",
        messages=[ApiMessage(role="user", content="hi")],
        tools=[
            ToolSpecRef(
                name="search",
                description="web",
                input_schema={"type": "object"},
            ),
        ],
    )
    tools = _to_gemini_tools(req)
    assert tools == [{
        "functionDeclarations": [{
            "name": "search",
            "description": "web",
            "parameters": {"type": "object"},
        }],
    }]


# ============================================================
# 2. 端到端 SSE 流
# ============================================================


@pytest.mark.asyncio
async def test_stream_minimal_text_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _build_stream([
        {
            "candidates": [{
                "content": {"parts": [{"text": "Hello "}]},
            }],
        },
        {
            "candidates": [{
                "content": {"parts": [{"text": "world!"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 5,
                "totalTokenCount": 105,
                "cachedContentTokenCount": 30,
            },
        },
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        # 默认 query 鉴权 → URL 含 key=
        assert "key=sk-gem" in str(req.url)
        assert "alt=sse" in str(req.url)
        return httpx.Response(200, content=body)

    _patch_httpx(monkeypatch, handler)

    session = GeminiSession(
        api_key="sk-gem",
        model="gemini-test",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    events = await _consume(session.stream(ApiRequest(
        model="gemini-test",
        messages=[ApiMessage(role="user", content="hi")],
    )))
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
    assert completed.data["usage"]["cache_read_input_tokens"] == 30


@pytest.mark.asyncio
async def test_stream_function_call_arrives_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """functionCall 不流式发 args delta —— 整体到达后一次性 emit done。"""
    body = _build_stream([
        {
            "candidates": [{
                "content": {"parts": [{
                    "functionCall": {
                        "name": "search",
                        "args": {"q": "hi"},
                    },
                }]},
                "finishReason": "TOOL_CALL",
            }],
            "usageMetadata": {
                "promptTokenCount": 50,
                "candidatesTokenCount": 10,
                "totalTokenCount": 60,
            },
        },
    ])

    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=body))

    session = GeminiSession(
        api_key="k",
        model="gem",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    events = await _consume(session.stream(ApiRequest(
        model="gem",
        messages=[ApiMessage(role="user", content="search hi")],
    )))
    # 不应有 tool_call_delta
    assert not any(e.kind == "tool_call_delta" for e in events)
    done_evs = [e for e in events if e.kind == "tool_call_done"]
    assert len(done_evs) == 1
    assert done_evs[0].data["name"] == "search"
    assert done_evs[0].data["arguments"] == '{"q": "hi"}'
    # finishReason=TOOL_CALL → end_turn=False
    assert events[-1].data["end_turn"] is False


@pytest.mark.asyncio
async def test_stream_header_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth_via=header 时 key 走 x-goog-api-key 头，不在 URL 里。"""
    body = _build_stream([
        {
            "candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        },
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        assert "key=" not in str(req.url)
        assert req.headers["x-goog-api-key"] == "sk-h"
        return httpx.Response(200, content=body)

    _patch_httpx(monkeypatch, handler)

    session = GeminiSession(
        api_key="sk-h",
        model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
        auth_via="header",
    )
    await _consume(session.stream(ApiRequest(
        model="g",
        messages=[ApiMessage(role="user", content="hi")],
    )))


# ============================================================
# 3. 错误路径
# ============================================================


@pytest.mark.asyncio
async def test_401_raises_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(401, content=b"bad key"),
    )
    session = GeminiSession(
        api_key="bad", model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(AuthenticationError):
        await _consume(session.stream(ApiRequest(
            model="g",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_429_raises_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(429, content=b"rate exceeded"),
    )
    session = GeminiSession(
        api_key="k", model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(RateLimitError):
        await _consume(session.stream(ApiRequest(
            model="g",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_500_raises_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(500, content=b"oops"),
    )
    session = GeminiSession(
        api_key="k", model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(ServerError):
        await _consume(session.stream(ApiRequest(
            model="g",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_connect_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")
    _patch_httpx(monkeypatch, handler)
    session = GeminiSession(
        api_key="k", model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )
    with pytest.raises(TransientNetworkError):
        await _consume(session.stream(ApiRequest(
            model="g",
            messages=[ApiMessage(role="user", content="hi")],
        )))


@pytest.mark.asyncio
async def test_cancel_midstream(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _build_stream([
        {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]},
    ])
    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=body))

    cancel = CancellationToken()
    cancel.cancel()
    session = GeminiSession(
        api_key="k", model="g",
        base_url="https://generativelanguage.googleapis.com",
        cancel=cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await _consume(session.stream(ApiRequest(
            model="g",
            messages=[ApiMessage(role="user", content="hi")],
        )))


# ============================================================
# 4. Client 层
# ============================================================


def test_client_default_constructor() -> None:
    c = GeminiClient(api_key="sk-x")
    assert c._default_model == "gemini-2.0-flash-exp"
    assert c._base_url == "https://generativelanguage.googleapis.com"
    assert c._auth_via == "query"


def test_client_session_returns_session() -> None:
    c = GeminiClient(api_key="sk-x")
    s = c.session(cancel=CancellationToken())
    assert isinstance(s, GeminiSession)


def test_client_record_cache_read() -> None:
    c = GeminiClient(api_key="sk-x")
    c.record_cache_read(50)
    assert c._previous_cache_read == 50
