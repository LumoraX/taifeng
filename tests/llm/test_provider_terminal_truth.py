"""Wave 3 复现:三家旧 provider 的流终止真相 / 传输层归一 / stop_reason 透传。

对应 openspec change ``wave3-provider-path-alignment``。每条用例先于修复写出,
改动前必须 FAIL(失败原因见 change tasks 1.1):
  a) gemini 流无 finishReason 即结束 → 伪造成 completed 成功
  b) gemini finishReason=SAFETY 零产出 → 伪造成空回复成功
  c) gemini MALFORMED_FUNCTION_CALL → 同上
  d) gemini / anthropic RemoteProtocolError 裸逃(只 catch NetworkError)
  e) anthropic 流无 message_stop 即结束 → 伪造成功
  f) anthropic stop_reason=refusal 零产出 → 伪造成功
  g) litellm finish_reason=content_filter 零产出 → 伪造成功
  h) 三家 completed 均不带原生 stop_reason
  i) gemini functionResponse.name 填的是 call_id 而非真实函数名
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from taifeng.llm.errors import (
    ContentFilterError,
    InvalidResponseError,
    TransientNetworkError,
)
from taifeng.llm.providers.anthropic_provider import AnthropicSession
from taifeng.llm.providers.gemini_provider import GeminiSession, _to_gemini_contents
from taifeng.llm.providers.litellm_provider import LiteLLMSession
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

# ───────────────────────── helpers ─────────────────────────


def _gem_sse(chunks: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode("utf-8")


def _ant_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events
    ).encode("utf-8")


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """把 httpx.AsyncClient 的 transport 换成 MockTransport。"""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _patch_httpx_raising(
    monkeypatch: pytest.MonkeyPatch, exc: Exception,
) -> None:
    """让 transport 在建流时抛指定异常（模拟网关中途断连）。"""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    _patch_httpx(monkeypatch, handler)


async def _consume(gen: Any) -> list[Any]:
    return [ev async for ev in gen]


def _gem_session() -> GeminiSession:
    return GeminiSession(
        api_key="k", model="gem",
        base_url="https://generativelanguage.googleapis.com",
        cancel=CancellationToken(),
    )


def _ant_session() -> AnthropicSession:
    return AnthropicSession(
        api_key="sk-ant", model="claude-x",
        base_url="https://api.anthropic.com", cancel=CancellationToken(),
    )


def _req(text: str = "hi") -> ApiRequest:
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content=text)])


def _terminal(events: list[Any]) -> Any:
    return next(ev for ev in events if ev.kind == "completed")


# ───────────────────────── a–c:gemini 终止真相 ─────────────────────────


async def test_gemini_stream_without_finish_reason_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a) 流吐了文本但从未给 finishReason（连接被掐断）→ 必须判失败,不得伪造成功。"""
    body = _gem_sse([{"candidates": [{"content": {"parts": [{"text": "半"}]}}]}])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(InvalidResponseError):
        await _consume(_gem_session().stream(_req()))


async def test_gemini_safety_finish_reason_raises_content_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """b) finishReason=SAFETY 且零产出 → ContentFilterError（不是成功空回复）。"""
    body = _gem_sse([{"candidates": [{"finishReason": "SAFETY", "content": {}}]}])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(ContentFilterError):
        await _consume(_gem_session().stream(_req()))


async def test_gemini_malformed_function_call_raises_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """c) finishReason=MALFORMED_FUNCTION_CALL → InvalidResponseError。"""
    body = _gem_sse([
        {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", "content": {}}]}
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(InvalidResponseError):
        await _consume(_gem_session().stream(_req()))


async def test_gemini_finish_reason_with_output_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向保护:异常 finishReason 但已有产出 → 不作废既有产出。"""
    body = _gem_sse([
        {"candidates": [{
            "content": {"parts": [{"functionCall": {"name": "echo", "args": {}}}]},
            "finishReason": "MAX_TOKENS",
        }]}
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    events = await _consume(_gem_session().stream(_req()))
    assert any(ev.kind == "tool_call_done" for ev in events)
    assert _terminal(events).data["stop_reason"] == "MAX_TOKENS"


# ───────────────────────── d:传输层归一 ─────────────────────────


async def test_gemini_remote_protocol_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """d1) RemoteProtocolError 属 ProtocolError（≠ NetworkError）→ 必须归瞬时网络错。"""
    _patch_httpx_raising(
        monkeypatch, httpx.RemoteProtocolError("Server disconnected"),
    )
    with pytest.raises(TransientNetworkError):
        await _consume(_gem_session().stream(_req()))


async def test_anthropic_remote_protocol_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """d2) anthropic 同病。"""
    _patch_httpx_raising(
        monkeypatch, httpx.RemoteProtocolError("Server disconnected"),
    )
    with pytest.raises(TransientNetworkError):
        await _consume(_ant_session().stream(_req()))


# ───────────────────────── e–f:anthropic 终止真相 ─────────────────────────


async def test_anthropic_stream_without_message_stop_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e) 没有 message_stop 就结束 → 判失败。"""
    body = _ant_sse([
        ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        ("content_block_delta", {
            "index": 0, "delta": {"type": "text_delta", "text": "半"},
        }),
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(InvalidResponseError):
        await _consume(_ant_session().stream(_req()))


async def test_anthropic_refusal_stop_reason_raises_content_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f) stop_reason=refusal 且零产出 → ContentFilterError。"""
    body = _ant_sse([
        ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
        ("message_delta", {"delta": {"stop_reason": "refusal"}, "usage": {}}),
        ("message_stop", {}),
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(ContentFilterError):
        await _consume(_ant_session().stream(_req()))


async def test_anthropic_completed_carries_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """h1) anthropic completed 必须带原生 stop_reason。"""
    body = _ant_sse([
        ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        ("content_block_delta", {
            "index": 0, "delta": {"type": "text_delta", "text": "答"},
        }),
        ("message_delta", {"delta": {"stop_reason": "max_tokens"}, "usage": {}}),
        ("message_stop", {}),
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    events = await _consume(_ant_session().stream(_req()))
    assert _terminal(events).data["stop_reason"] == "max_tokens"


async def test_gemini_completed_carries_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """h2) gemini completed 带原生 finishReason。"""
    body = _gem_sse([
        {"candidates": [{
            "content": {"parts": [{"text": "答"}]}, "finishReason": "STOP",
        }]}
    ])
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, content=body))
    events = await _consume(_gem_session().stream(_req()))
    assert _terminal(events).data["stop_reason"] == "STOP"


# ───────────────────────── g:litellm 终止真相 ─────────────────────────


def _litellm_session() -> LiteLLMSession:
    return LiteLLMSession(
        model="m", api_key="k", base_url=None, cancel=CancellationToken(),
    )


def _patch_acompletion(
    monkeypatch: pytest.MonkeyPatch, chunks: list[dict[str, Any]],
) -> None:
    """把 litellm.acompletion 换成回放给定 chunk 序列的假实现。"""
    import litellm

    async def fake_acompletion(**_kw: Any) -> Any:
        async def gen() -> Any:
            for c in chunks:
                yield c

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)


async def test_litellm_content_filter_finish_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """g) finish_reason=content_filter 且零产出 → ContentFilterError。"""
    _patch_acompletion(monkeypatch, [
        {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
    ])
    with pytest.raises(ContentFilterError):
        await _consume(_litellm_session().stream(_req()))


async def test_litellm_stream_without_finish_reason_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e2) litellm 流没有任何 finish_reason 就结束 → 判失败。"""
    _patch_acompletion(monkeypatch, [
        {"choices": [{"delta": {"content": "半"}}]},
    ])
    with pytest.raises(InvalidResponseError):
        await _consume(_litellm_session().stream(_req()))


async def test_litellm_completed_carries_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """h3) litellm completed 带原生 finish_reason。"""
    _patch_acompletion(monkeypatch, [
        {"choices": [{"delta": {"content": "答"}, "finish_reason": "length"}]},
    ])
    events = await _consume(_litellm_session().stream(_req()))
    assert _terminal(events).data["stop_reason"] == "length"


# ───────────────────────── i:gemini functionResponse 函数名 ─────────────────────────


def test_gemini_function_response_uses_real_function_name() -> None:
    """i) tool 消息的 functionResponse.name 必须是函数名,不是 call_id。"""
    req = ApiRequest(
        model="gem",
        messages=[
            ApiMessage(role="user", content="读文件"),
            ApiMessage(
                role="assistant", content="",
                tool_calls=[{
                    "id": "call_1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            ),
            ApiMessage(role="tool", content="文件内容", tool_call_id="call_1"),
        ],
    )
    _system, contents = _to_gemini_contents(req)
    fr = next(
        part["functionResponse"]
        for c in contents for part in c.get("parts", [])
        if "functionResponse" in part
    )
    assert fr["name"] == "read_file", f"应为真实函数名,实得 {fr['name']!r}"
