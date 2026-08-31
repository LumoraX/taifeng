"""provider 层 finish_reason 异常终止保护。

回归用例来源：真实抓包 —— Gemini 3.1 pro 经 openai-compat 网关对某些子 skill prompt
返回 ``finish_reason="content_filter"`` + 空 content + 0 token。旧实现完全不读
finish_reason，把「被安全过滤拦截」伪造成「成功的空回复」（silent fallback，违反 R 线）。
本用例钉死：provider 必须把 content_filter 暴露为 ``ContentFilterError`` 并 emit error 事件。
"""

from __future__ import annotations

import httpx
import pytest

from taifeng.llm.errors import ContentFilterError, InvalidResponseError
from taifeng.llm.providers import OpenAICompatClient
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

# 复刻真实抓到的 content_filter 流：先一个空 role chunk，再一个 finish_reason=content_filter
# 的空 content chunk，最后 usage（completion_tokens=0）+ [DONE]。
CONTENT_FILTER_SSE = (
    b'data: {"choices":[{"delta":{"content":"","role":"assistant"},"finish_reason":null,"index":0}]}\n\n'
    b'data: {"choices":[{"delta":{"content":""},"finish_reason":"content_filter","index":0}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":910,"completion_tokens":0,"total_tokens":910}}\n\n'
    b"data: [DONE]\n\n"
)

MALFORMED_FUNCTION_CALL_SSE = (
    b'data: {"choices":[{"delta":{"role":"assistant","extra_content":{"google":{"thought_signature":"sig-1"}}},"finish_reason":"function_call_filter: MALFORMED_FUNCTION_CALL","index":0}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":910,"completion_tokens":0,"total_tokens":910}}\n\n'
    b"data: [DONE]\n\n"
)


async def _collect(sse_bytes: bytes) -> list:
    """用 MockTransport 喂入指定 SSE，收集 provider 产出的事件（异常向上抛）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_bytes, headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def patched(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    httpx.AsyncClient = patched  # type: ignore[misc]
    try:
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1", api_key="sk-test", model="m"
        )
        sess = client.session(cancel=CancellationToken())
        events = []
        async with sess as s:
            req = ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])
            async for ev in s.stream(req):
                events.append(ev)
        return events
    finally:
        httpx.AsyncClient = orig  # type: ignore[misc]


async def test_content_filter_finish_reason_raises() -> None:
    """finish_reason=content_filter + 空 content → 抛 ContentFilterError，且先 emit error 事件。"""
    captured: list = []
    with pytest.raises(ContentFilterError):
        # 手动迭代以便在异常前捕获已 emit 的事件
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=CONTENT_FILTER_SSE, headers={"content-type": "text/event-stream"}
            )

        transport = httpx.MockTransport(handler)
        orig = httpx.AsyncClient

        def patched(*args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        httpx.AsyncClient = patched  # type: ignore[misc]
        try:
            client = OpenAICompatClient(
                base_url="https://api.example.com/v1", api_key="sk-test", model="m"
            )
            sess = client.session(cancel=CancellationToken())
            async with sess as s:
                req = ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])
                async for ev in s.stream(req):
                    captured.append(ev)
        finally:
            httpx.AsyncClient = orig  # type: ignore[misc]

    # 异常前应已 emit 一个 error 事件，kind=content_filter，且没有伪造 completed
    kinds = [e.kind for e in captured]
    assert "error" in kinds, f"应 emit error 事件，实际事件={kinds}"
    err_ev = next(e for e in captured if e.kind == "error")
    assert err_ev.data.get("kind") == "content_filter"
    assert "completed" not in kinds, "content_filter 不得伪造成功 completed"


async def test_malformed_function_call_finish_reason_raises() -> None:
    """Gemini function_call_filter 截停 → 抛 InvalidResponseError，不得伪造成空完成。"""
    captured: list = []
    with pytest.raises(InvalidResponseError):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=MALFORMED_FUNCTION_CALL_SSE,
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.MockTransport(handler)
        orig = httpx.AsyncClient

        def patched(*args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            return orig(*args, **kwargs)

        httpx.AsyncClient = patched  # type: ignore[misc]
        try:
            client = OpenAICompatClient(
                base_url="https://api.example.com/v1", api_key="sk-test", model="m"
            )
            sess = client.session(cancel=CancellationToken())
            async with sess as s:
                req = ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])
                async for ev in s.stream(req):
                    captured.append(ev)
        finally:
            httpx.AsyncClient = orig  # type: ignore[misc]

    kinds = [e.kind for e in captured]
    assert "error" in kinds
    err_ev = next(e for e in captured if e.kind == "error")
    assert err_ev.data.get("kind") == "invalid_response"
    assert "function_call_filter" in err_ev.data.get("message", "")
    assert "completed" not in kinds


async def test_normal_stop_still_completes() -> None:
    """对照：正常 finish_reason=stop + 有 content → 照常 completed，不受影响。"""
    normal_sse = (
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null,"index":0}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
        b"data: [DONE]\n\n"
    )
    events = await _collect(normal_sse)
    kinds = [e.kind for e in events]
    assert "completed" in kinds
    text = "".join(e.data.get("text", "") for e in events if e.kind == "text_delta")
    assert text == "hello"
