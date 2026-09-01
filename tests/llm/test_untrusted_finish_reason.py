"""端点 finish_reason 不可信时的终止分类（``trust_finish_reason=False``）。

**背景（真实抓包，2026-08-31）**：new-api 网关（``X-New-Api-Version: v1.0.0-rc.25``）
把 Gemini 上游一切**未枚举**的 ``finishReason`` 一律塌缩成 OpenAI 的
``content_filter`` —— 见 ``relaykit/relayconvert/internal/gemini_chat/to_oai_chat_resp.go``
的 ``default:`` 分支（非流式 :172 / 流式 :224）。同一网关的 Gemini **原生透传**端点
上喂同一份载荷，失败时真实原因是 ``MALFORMED_FUNCTION_CALL``（实测 6 次中 3 次），
``blockReason`` 恒为 ``None`` —— **根本不是安全拦截**，而是模型在并发 tool call 上
的瞬时抖动，重试即过。

**后果**：taifeng 把 ``content_filter`` 判为终态不可重试（``ContentFilterError``），
一次瞬时抖动就把整个 turn 判死（业务侧表现为「专科分析失败(content_filter)」且
提示不可恢复）。

本用例钉死的契约：接入方**显式声明**端点 finish_reason 不可信时（``trust_finish_reason=False``），
零产出的 ``content_filter`` 必须归入**可重试**的 ``UnreliableFinishError``；
未声明（默认 ``True``）时行为逐字不变，真安全拦截仍是终态。

不做启发式猜测 —— 是否可信由接入方按端点事实声明，内核不嗅探。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from taifeng.llm.errors import ContentFilterError, UnreliableFinishError
from taifeng.llm.providers import OpenAICompatClient
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

# 复刻真实抓到的 content_filter 流：空 role chunk → finish_reason=content_filter 的空
# content chunk → usage(completion_tokens=0) → [DONE]。即「零产出被判停」。
CONTENT_FILTER_SSE = (
    b'data: {"choices":[{"delta":{"content":"","role":"assistant"},'
    b'"finish_reason":null,"index":0}]}\n\n'
    b'data: {"choices":[{"delta":{"content":""},"finish_reason":"content_filter","index":0}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":910,"completion_tokens":0,'
    b'"total_tokens":910}}\n\n'
    b"data: [DONE]\n\n"
)

# 带 tool_call 的正常流：即便流末 finish_reason 异常，也**有产出**，不得判失败。
TOOL_CALL_THEN_CONTENT_FILTER_SSE = (
    b'data: {"choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-1",'
    b'"function":{"name":"spawn_skill","arguments":"{}"}}]},"finish_reason":null,"index":0}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"content_filter","index":0}]}\n\n'
    b"data: [DONE]\n\n"
)


async def _collect(sse_bytes: bytes, into: list | None = None, **client_kwargs: Any) -> list:
    """用 MockTransport 喂入指定 SSE，收集 provider 产出的事件（异常向上抛）。

    Args:
        sse_bytes: 要喂给 provider 的完整 SSE 字节流。
        into: 事件收集容器；传入后**异常路径下也能拿到已 emit 的事件**（异常前
            emit 的 error 事件正是要断言的对象，用返回值拿不到）。
        client_kwargs: 透传给 ``OpenAICompatClient`` 的额外构造参数。
    Returns:
        provider 在抛异常前（或正常结束前）产出的 ResponseEvent 列表。
    """

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
    events: list = into if into is not None else []
    try:
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="m",
            **client_kwargs,
        )
        sess = client.session(cancel=CancellationToken())
        async with sess as s:
            req = ApiRequest(model="m", messages=[ApiMessage(role="user", content="hi")])
            async for ev in s.stream(req):
                events.append(ev)
        return events
    finally:
        httpx.AsyncClient = orig  # type: ignore[misc]


async def test_untrusted_endpoint_maps_zero_output_content_filter_to_retryable() -> None:
    """声明 finish_reason 不可信 → 零产出 content_filter 抛可重试的 UnreliableFinishError。"""
    with pytest.raises(UnreliableFinishError) as exc_info:
        await _collect(CONTENT_FILTER_SSE, trust_finish_reason=False)

    err = exc_info.value
    assert err.retryable is True, "网关标签不可信时该失败必须是可重试的"
    assert err.failure_class == "provider_unreliable_finish"
    # 消息里要留下「原始 finish_reason 是什么」，否则排障时无从区分真假安全拦截
    assert "content_filter" in str(err)


async def test_untrusted_endpoint_emits_retryable_error_event() -> None:
    """抛异常前必须先 emit error 事件，且 retryable=True（与 HTTP 错误路径一致）。"""
    events: list = []
    with pytest.raises(UnreliableFinishError):
        await _collect(CONTENT_FILTER_SSE, into=events, trust_finish_reason=False)

    kinds = [e.kind for e in events]
    assert "error" in kinds, f"应 emit error 事件，实际事件={kinds}"
    err_ev = next(e for e in events if e.kind == "error")
    assert err_ev.data.get("kind") == "unreliable_finish"
    assert err_ev.data.get("retryable") is True
    assert "completed" not in kinds, "异常终止不得伪造成功 completed"


async def test_trusted_endpoint_keeps_content_filter_terminal() -> None:
    """默认（未声明不可信）行为逐字不变：仍抛终态 ContentFilterError。

    回归护栏 —— 真安全拦截在可信端点上必须继续判死，不得被本变更放宽。
    """
    with pytest.raises(ContentFilterError):
        await _collect(CONTENT_FILTER_SSE)


async def test_untrusted_endpoint_keeps_tool_call_stream_successful() -> None:
    """零误伤：有 tool_call 产出的流即便流末 finish_reason=content_filter 也不判失败。"""
    events = await _collect(
        TOOL_CALL_THEN_CONTENT_FILTER_SSE, trust_finish_reason=False
    )

    kinds = [e.kind for e in events]
    assert "error" not in kinds, f"有产出的流不得判失败，实际事件={kinds}"
    assert "tool_call_done" in kinds
    assert "completed" in kinds
