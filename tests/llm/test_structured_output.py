"""llm-structured-output 单测 —— 覆盖 3 provider × 多 scenario。

设计：
    - mock provider：纯单测
    - openai_compat：用 httpx.MockTransport 注入 SSE
    - litellm：mock litellm.acompletion 返回 async iterator（避开 litellm 依赖）
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from taifeng.llm.providers import SimClient
from taifeng.llm.providers.sim import SimTurn
from taifeng.llm.providers.openai_compat import OpenAICompatClient, OpenAICompatSession
from taifeng.llm.types import (
    ApiMessage,
    ApiRequest,
    ResponseFormatSpec,
    TokenUsage,
)
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

# ====================================================================
# 1. types：默认 + 字段校验
# ====================================================================

def test_api_request_response_format_default_none() -> None:
    """ApiRequest 默认 response_format 为 None（向后兼容）。"""
    r = ApiRequest(model="x", messages=[ApiMessage(role="user", content="hi")])
    assert r.response_format is None


def test_response_format_spec_strict_defaults_true() -> None:
    """ResponseFormatSpec.strict 默认 True；name + json_schema 必填。"""
    spec = ResponseFormatSpec(
        name="UserProfile",
        json_schema={"type": "object", "properties": {"id": {"type": "integer"}}},
    )
    assert spec.strict is True
    assert spec.name == "UserProfile"
    assert spec.json_schema["type"] == "object"


# ====================================================================
# 2. openai_compat：payload 字段 + 流末 emit
# ====================================================================

def test_openai_compat_payload_adds_response_format_field() -> None:
    """_build_payload：response_format 非 None 时翻译到 OpenAI 原生格式。"""
    sess = OpenAICompatSession(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        cancel=CancellationToken(),
    )
    spec = ResponseFormatSpec(
        name="X", json_schema={"type": "object"}, strict=True,
    )
    req = ApiRequest(
        model="gpt-4o-mini",
        messages=[ApiMessage(role="user", content="hi")],
        response_format=spec,
    )
    payload = sess._build_payload(req)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "X",
            "schema": {"type": "object"},
            "strict": True,
        },
    }


def test_openai_compat_payload_no_response_format_when_none() -> None:
    """response_format=None → payload 不含该字段（向后兼容）。"""
    sess = OpenAICompatSession(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        cancel=CancellationToken(),
    )
    req = ApiRequest(
        model="gpt-4o-mini",
        messages=[ApiMessage(role="user", content="hi")],
    )
    payload = sess._build_payload(req)
    assert "response_format" not in payload


def _build_sse(text_chunks: list[str]) -> bytes:
    """构造 OpenAI SSE 响应 —— 多个 text delta + usage + [DONE]。"""
    lines = []
    for chunk in text_chunks:
        body = json.dumps({"choices": [{"delta": {"content": chunk}}]})
        lines.append(f"data: {body}\n\n")
    # 最后一帧 usage
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    last = json.dumps({"choices": [{"delta": {}}], "usage": usage})
    lines.append(f"data: {last}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


async def _run_openai_compat_stream(
    *,
    sse_bytes: bytes,
    response_format: ResponseFormatSpec | None,
) -> list:
    """跑一次 openai_compat stream 收集事件 —— 使用 httpx.MockTransport。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_bytes,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def patched(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    httpx.AsyncClient = patched  # type: ignore[misc]
    events = []
    try:
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-4o-mini",
        )
        sess = client.session(cancel=CancellationToken())
        async with sess as s:
            req = ApiRequest(
                model="gpt-4o-mini",
                messages=[ApiMessage(role="user", content="hi")],
                response_format=response_format,
            )
            async for ev in s.stream(req):
                events.append(ev)
    finally:
        httpx.AsyncClient = orig  # type: ignore[misc]
    return events


async def test_openai_compat_emits_structured_output_on_valid_json() -> None:
    """有效 JSON 响应 + 带 response_format → emit structured_output。"""
    spec = ResponseFormatSpec(name="X", json_schema={"type": "object"})
    sse = _build_sse(['{"id":1,', '"name":"alice"}'])

    events = await _run_openai_compat_stream(sse_bytes=sse, response_format=spec)
    kinds = [e.kind for e in events]

    assert "structured_output" in kinds
    so = next(e for e in events if e.kind == "structured_output")
    assert so.data["parsed"] == {"id": 1, "name": "alice"}
    assert so.data["raw_text"] == '{"id":1,"name":"alice"}'

    # structured_output 必须在 completed 之前
    assert kinds.index("structured_output") < kinds.index("completed")


async def test_openai_compat_emits_parse_error_on_invalid_json() -> None:
    """非 JSON 响应 + 带 response_format → emit error(kind='parse_error')；仍 emit completed。"""
    spec = ResponseFormatSpec(name="X", json_schema={"type": "object"})
    sse = _build_sse(["sorry, ", "cannot comply"])

    events = await _run_openai_compat_stream(sse_bytes=sse, response_format=spec)
    kinds = [e.kind for e in events]

    assert "structured_output" not in kinds
    err = [e for e in events if e.kind == "error"]
    assert err, "expected error event"
    assert err[0].data["kind"] == "parse_error"
    assert err[0].data["retryable"] is False
    assert "completed" in kinds


async def test_openai_compat_no_response_format_skips_structured_output() -> None:
    """response_format=None → 即使响应是 JSON 也不 emit structured_output（向后兼容）。"""
    sse = _build_sse(['{"a":1}'])

    events = await _run_openai_compat_stream(sse_bytes=sse, response_format=None)
    kinds = [e.kind for e in events]

    assert "structured_output" not in kinds
    assert "completed" in kinds


# ====================================================================
# 3. litellm：kwargs 字段
# ====================================================================

class _StubAcompletion:
    """伪装 litellm.acompletion —— 返回一个 async iterator chunks。"""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.captured_kwargs: dict = {}

    async def __call__(self, **kwargs) -> AsyncIterator[dict]:  # noqa: ANN003
        self.captured_kwargs = kwargs

        async def gen():
            for ch in self.chunks:
                yield ch

        return gen()


async def test_litellm_passes_response_format_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLMSession.stream 把 response_format 翻译到 litellm.acompletion kwargs。"""
    import sys
    import types

    stub_module = types.ModuleType("litellm")
    chunks = [
        {"choices": [{"delta": {"content": '{"a":1}'}}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
    ]
    stub = _StubAcompletion(chunks)
    stub_module.acompletion = stub  # type: ignore[attr-defined]
    stub_module.suppress_debug_info = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub_module)

    from taifeng.llm.providers.litellm_provider import LiteLLMClient

    spec = ResponseFormatSpec(
        name="X", json_schema={"type": "object"}, strict=True,
    )
    client = LiteLLMClient(api_key="sk-test", model="gpt-4o-mini")
    sess = client.session(cancel=CancellationToken())
    events = []
    async with sess as s:
        req = ApiRequest(
            model="gpt-4o-mini",
            messages=[ApiMessage(role="user", content="hi")],
            response_format=spec,
        )
        async for ev in s.stream(req):
            events.append(ev)

    # kwargs 含 response_format
    assert stub.captured_kwargs.get("response_format") == {
        "type": "json_schema",
        "json_schema": {
            "name": "X",
            "schema": {"type": "object"},
            "strict": True,
        },
    }
    # 且 emit 了 structured_output
    kinds = [e.kind for e in events]
    assert "structured_output" in kinds
    so = next(e for e in events if e.kind == "structured_output")
    assert so.data["parsed"] == {"a": 1}


# ====================================================================
# 4. mock：structured 字段
# ====================================================================

async def test_mock_session_emits_structured_output_when_configured() -> None:
    """SimTurn.structured 配置 + ApiRequest 带 response_format → emit。"""
    client = SimClient(turns=[
        SimTurn(text="", structured={"x": 1}, usage=TokenUsage(input_tokens=10)),
    ])
    spec = ResponseFormatSpec(name="X", json_schema={"type": "object"})
    sess = client.session(cancel=CancellationToken())
    events = []
    async with sess as s:
        req = ApiRequest(
            model="mock-model",
            messages=[ApiMessage(role="user", content="hi")],
            response_format=spec,
        )
        async for ev in s.stream(req):
            events.append(ev)

    kinds = [e.kind for e in events]
    assert "structured_output" in kinds
    so = next(e for e in events if e.kind == "structured_output")
    assert so.data["parsed"] == {"x": 1}
    assert so.data["raw_text"] == '{"x": 1}'
    # structured_output 在 completed 之前
    assert kinds.index("structured_output") < kinds.index("completed")


async def test_mock_session_no_emit_when_structured_unset() -> None:
    """SimTurn 未设 structured（默认 None）+ response_format → 不 emit。"""
    client = SimClient(turns=[SimTurn(text="hi")])
    spec = ResponseFormatSpec(name="X", json_schema={"type": "object"})
    sess = client.session(cancel=CancellationToken())
    events = []
    async with sess as s:
        req = ApiRequest(
            model="mock-model",
            messages=[ApiMessage(role="user", content="hi")],
            response_format=spec,
        )
        async for ev in s.stream(req):
            events.append(ev)

    kinds = [e.kind for e in events]
    assert "structured_output" not in kinds


async def test_mock_session_no_emit_when_response_format_none() -> None:
    """SimTurn 设了 structured 但请求 response_format=None → 不 emit（保险）。"""
    client = SimClient(turns=[
        SimTurn(text="", structured={"x": 1}),
    ])
    sess = client.session(cancel=CancellationToken())
    events = []
    async with sess as s:
        req = ApiRequest(
            model="mock-model",
            messages=[ApiMessage(role="user", content="hi")],
        )
        async for ev in s.stream(req):
            events.append(ev)

    kinds = [e.kind for e in events]
    assert "structured_output" not in kinds
