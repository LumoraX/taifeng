"""OpenAICompatClient —— 用 httpx MockTransport 模拟 SSE。"""

from __future__ import annotations

import httpx
import pytest

from taifeng.llm.providers import OpenAICompatClient
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
