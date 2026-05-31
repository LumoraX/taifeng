"""`DeepSeekClient` 单元测试。

覆盖 spec ``llm-provider-native`` 的 Requirement「DeepSeekClient 作为
OpenAICompatClient 薄子类」全部 6 个 Scenario：
    - 默认 base_url 与 model
    - 复用 OpenAICompat 流式逻辑
    - DeepSeek cache 字段（prompt_cache_hit_tokens）→ cache_read_input_tokens
    - R1 推理模式发 reasoning_delta
    - extract_usage_openai_family 优先级（OpenAI 标准字段）—— 已在
      test_extract_usage_shared.py 覆盖
    - extract_usage_openai_family 优先级（DeepSeek 字段）—— 同上
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from taifeng.llm.providers.deepseek_provider import DeepSeekClient
from taifeng.llm.providers.openai_compat import OpenAICompatClient, OpenAICompatSession
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

# ============================================================
# helpers
# ============================================================


def _sse_chunk(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _build_stream(chunks: list[dict[str, Any]]) -> bytes:
    s = "".join(_sse_chunk(c) for c in chunks) + "data: [DONE]\n\n"
    return s.encode("utf-8")


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
# 1. 构造默认值
# ============================================================


def test_default_base_url_and_model() -> None:
    c = DeepSeekClient(api_key="sk-x")
    assert c._base_url == "https://api.deepseek.com"
    assert c._default_model == "deepseek-chat"


def test_override_to_reasoner_model() -> None:
    c = DeepSeekClient(api_key="sk-x", model="deepseek-reasoner")
    assert c._default_model == "deepseek-reasoner"


def test_is_subclass_of_openai_compat() -> None:
    """DeepSeekClient 必须是 OpenAICompatClient 的子类，确保协议契合。"""
    assert issubclass(DeepSeekClient, OpenAICompatClient)
    c = DeepSeekClient(api_key="sk-x")
    s = c.session(cancel=CancellationToken())
    assert isinstance(s, OpenAICompatSession)


# ============================================================
# 2. 复用 OpenAICompat 流式骨架
# ============================================================


@pytest.mark.asyncio
async def test_stream_reuses_openai_compat_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑一次最小 turn，验证事件序列与 OpenAICompatSession 同构。"""
    body = _build_stream([
        {
            "choices": [{
                "index": 0,
                "delta": {"content": "Hello "},
            }],
        },
        {
            "choices": [{
                "index": 0,
                "delta": {"content": "world!"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
            },
        },
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        # 验证 base_url 是 DeepSeek 官方
        assert str(req.url).startswith("https://api.deepseek.com")
        assert req.headers["Authorization"] == "Bearer sk-ds"
        payload = json.loads(req.content)
        assert payload["model"] == "deepseek-chat"
        return httpx.Response(200, content=body)

    _patch_httpx(monkeypatch, handler)

    client = DeepSeekClient(api_key="sk-ds")
    session = client.session(cancel=CancellationToken())
    events = await _consume(session.stream(ApiRequest(
        model="deepseek-chat",
        messages=[ApiMessage(role="user", content="hi")],
    )))
    kinds = [e.kind for e in events]
    assert kinds[0] == "created"
    assert kinds[1] == "server_model"
    assert "text_delta" in kinds
    assert kinds[-1] == "completed"


# ============================================================
# 3. DeepSeek 特有 cache 字段精准映射
# ============================================================


@pytest.mark.asyncio
async def test_deepseek_cache_fields_extracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage 含 prompt_cache_hit_tokens → cache_read_input_tokens 正确填。"""
    body = _build_stream([
        {
            "choices": [{
                "index": 0,
                "delta": {"content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            },
        },
    ])

    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=body))

    client = DeepSeekClient(api_key="sk-x")
    session = client.session(cancel=CancellationToken())
    events = await _consume(session.stream(ApiRequest(
        model="deepseek-chat",
        messages=[ApiMessage(role="user", content="hi")],
    )))
    completed = events[-1]
    assert completed.data["usage"]["input_tokens"] == 1000
    assert completed.data["usage"]["output_tokens"] == 200
    assert completed.data["usage"]["cache_read_input_tokens"] == 800
    # miss 不映射但走 raw
    assert (
        completed.data["usage"]["raw"]["prompt_cache_miss_tokens"] == 200
    )


# ============================================================
# 4. R1 推理模式 —— reasoning_content delta → reasoning_delta 事件
# ============================================================


@pytest.mark.asyncio
async def test_r1_reasoning_content_emits_reasoning_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deepseek-reasoner 的 SSE delta 含 reasoning_content，
    应 emit reasoning_delta 事件（复用 OpenAICompatSession 现有逻辑）。"""
    body = _build_stream([
        {
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "Let me think... "},
            }],
        },
        {
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "the answer is 42."},
            }],
        },
        {
            "choices": [{
                "index": 0,
                "delta": {"content": "42"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 100,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 80},
            },
        },
    ])

    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=body))

    client = DeepSeekClient(api_key="sk-x", model="deepseek-reasoner")
    session = client.session(cancel=CancellationToken())
    events = await _consume(session.stream(ApiRequest(
        model="deepseek-reasoner",
        messages=[ApiMessage(role="user", content="what is 6*7?")],
    )))
    reasoning_evs = [e for e in events if e.kind == "reasoning_delta"]
    text_evs = [e for e in events if e.kind == "text_delta"]
    assert len(reasoning_evs) == 2
    assert reasoning_evs[0].data["delta"] == "Let me think... "
    assert reasoning_evs[1].data["delta"] == "the answer is 42."
    assert len(text_evs) == 1
    assert text_evs[0].data["text"] == "42"
    # reasoning_tokens 透传到 TokenUsage
    completed = events[-1]
    assert completed.data["usage"]["reasoning_tokens"] == 80
