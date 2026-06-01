"""OpenAICompatClient —— 用 httpx MockTransport 模拟 SSE。"""

from __future__ import annotations

import httpx
import pytest

from taifeng.llm.providers import OpenAICompatClient, OpenAICompatSession
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken


SSE_RESPONSE = (
    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
    b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"prompt_tokens_details":{"cached_tokens":8}}}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.mark.asyncio
async def test_openai_compat_streams_text() -> None:
    """模拟 OpenAI v1 chat/completions SSE，断言事件序列正确。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_RESPONSE, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)

    # Monkey-patch httpx.AsyncClient default to use our transport
    import taifeng.llm.providers.openai_compat as mod
    orig_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    mod.httpx = httpx  # ensure import resolved
    # Patch at call site by monkey-patching httpx.AsyncClient temporarily
    httpx.AsyncClient = patched  # type: ignore[misc]

    try:
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-4o-mini",
        )
        sess = client.session(cancel=CancellationToken())
        events = []
        async with sess as s:
            req = ApiRequest(model="gpt-4o-mini", messages=[ApiMessage(role="user", content="hi")])
            async for ev in s.stream(req):
                events.append(ev)
    finally:
        httpx.AsyncClient = orig_async_client  # type: ignore[misc]

    kinds = [e.kind for e in events]
    assert "created" in kinds
    assert "text_delta" in kinds
    assert "completed" in kinds
    text = "".join(e.data.get("text", "") for e in events if e.kind == "text_delta")
    assert text == "hello world"
    # usage 解析
    completed_ev = [e for e in events if e.kind == "completed"][0]
    usage = completed_ev.data["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_input_tokens"] == 8


# === 空 key 鉴权头处理（本地 Ollama / LM Studio 等无需 key 的端点）===========
# 回归：空 api_key 旧实现会发出非法的 "Bearer "（带尾空格）→ httpx LocalProtocolError。
# 修复后：空/空白 key 直接省略 Authorization 头（本地端点正常工作；真实服务端干净 401）。


def _session(api_key: str, extra_headers: dict[str, str] | None = None) -> OpenAICompatSession:
    """构造一个 OpenAICompatSession 用于检查鉴权头（不发请求）。"""
    return OpenAICompatSession(
        base_url="https://api.example.com/v1",
        api_key=api_key,
        model="gpt-4o-mini",
        cancel=CancellationToken(),
        extra_headers=extra_headers,
    )


def test_empty_api_key_omits_authorization_header() -> None:
    """空 key（如本地 Ollama）应省略 Authorization，而非发出非法的 'Bearer '。"""
    sess = _session("")
    assert "Authorization" not in sess._headers
    assert sess._headers["Content-Type"] == "application/json"


def test_whitespace_api_key_omits_authorization_header() -> None:
    """纯空白 key 同样视为无 key → 省略 Authorization。"""
    sess = _session("   ")
    assert "Authorization" not in sess._headers


def test_nonempty_api_key_sets_bearer_header() -> None:
    """正常 key → 标准 Bearer 头。"""
    sess = _session("sk-test")
    assert sess._headers["Authorization"] == "Bearer sk-test"


def test_extra_headers_can_inject_auth_even_with_empty_key() -> None:
    """网关自定义鉴权头：即便 api_key 为空，extra_headers 仍可注入 Authorization。"""
    sess = _session("", extra_headers={"Authorization": "Custom token123"})
    assert sess._headers["Authorization"] == "Custom token123"
