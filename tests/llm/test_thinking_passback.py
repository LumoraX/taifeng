"""thinking-passback —— 原生 Anthropic / Gemini 的思考块与签名回传。

回归点：Anthropic 开 extended thinking 后，工具续传的 assistant 消息必须以上一轮
thinking 块（含签名）原样开头；Gemini thinking 模型的 functionCall part 必须带回
thoughtSignature。此前两家原生 provider 都只解析 text / tool 增量，签名丢失。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import taifeng
from taifeng.llm.errors import InvalidRequestError
from taifeng.llm.providers.anthropic_provider import (
    AnthropicClient,
    AnthropicSession,
    _to_anthropic_messages,
)
from taifeng.llm.providers.gemini_provider import GeminiSession, _to_gemini_contents
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _patch_httpx(monkeypatch: pytest.MonkeyPatch,
                 handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """把 httpx.AsyncClient 替换成挂 MockTransport 的版本。"""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _anthropic_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _thinking_tool_stream() -> bytes:
    """thinking（含签名）+ redacted_thinking + tool_use 的一次完整响应。"""
    return _anthropic_sse([
        ("message_start", {"message": {"usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {
            "type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"index": 0, "delta": {
            "type": "thinking_delta", "thinking": "先读一下"}}),
        ("content_block_delta", {"index": 0, "delta": {
            "type": "thinking_delta", "thinking": "子 skill"}}),
        ("content_block_delta", {"index": 0, "delta": {
            "type": "signature_delta", "signature": "SIG-1"}}),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": {
            "type": "redacted_thinking", "data": "ENCRYPTED"}}),
        ("content_block_stop", {"index": 1}),
        ("content_block_start", {"index": 2, "content_block": {
            "type": "tool_use", "id": "tu_1", "name": "read_skill"}}),
        ("content_block_delta", {"index": 2, "delta": {
            "type": "input_json_delta", "partial_json": '{"skill_id": "style-checker"}'}}),
        ("content_block_stop", {"index": 2}),
        ("message_delta", {"delta": {"stop_reason": "tool_use"},
                           "usage": {"output_tokens": 30}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _text_stream(text: str) -> bytes:
    return _anthropic_sse([
        ("message_start", {"message": {"usage": {"input_tokens": 20, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _session(**kwargs: Any) -> AnthropicSession:
    return AnthropicSession(api_key="k", model="claude", base_url="https://api.anthropic.com",
                            cancel=CancellationToken(), **kwargs)


async def test_anthropic_stream_emits_reasoning_and_signed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking 文本 → reasoning_delta；thinking 块（含签名）与 redacted 块 → reasoning_state。"""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, content=_thinking_tool_stream()))
    events = [ev async for ev in _session().stream(ApiRequest(
        model="claude", messages=[ApiMessage(role="user", content="hi")]))]
    kinds = [e.kind for e in events]

    assert [e.data["delta"] for e in events if e.kind == "reasoning_delta"] == ["先读一下", "子 skill"]
    assert "text_delta" not in kinds  # 思考不混进正文
    state = next(e for e in events if e.kind == "reasoning_state").data["state"]
    assert state == {"anthropic": {"blocks": [
        {"type": "thinking", "thinking": "先读一下子 skill", "signature": "SIG-1"},
        {"type": "redacted_thinking", "data": "ENCRYPTED"},
    ]}}
    assert kinds.index("reasoning_state") < kinds.index("tool_call_done")


def test_anthropic_assistant_message_prepends_thinking_blocks() -> None:
    """回传：assistant content 以 thinking 块开头，其后才是文本与 tool_use。"""
    blocks = [{"type": "thinking", "thinking": "t", "signature": "S"}]
    req = ApiRequest(model="claude", messages=[
        ApiMessage(role="user", content="hi"),
        ApiMessage(role="assistant", content="好", reasoning_state={"anthropic": {"blocks": blocks}},
                   tool_calls=[{"id": "tu", "function": {"name": "x", "arguments": "{}"}}]),
    ])
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert [b["type"] for b in msgs[1]["content"]] == ["thinking", "text", "tool_use"]
    assert msgs[1]["content"][0]["signature"] == "S"


def test_anthropic_foreign_or_missing_state_adds_nothing() -> None:
    """其他 provider 的状态 / 无状态 → 不注入任何块（旧形状不变）。"""
    req = ApiRequest(model="claude", messages=[
        ApiMessage(role="assistant", content="好", reasoning_state={"google": {"x": 1}}),
    ])
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert msgs[0]["content"] == [{"type": "text", "text": "好"}]


def test_anthropic_thinking_config_in_payload() -> None:
    """开预算 → payload 带 thinking，max_tokens 自动高于预算。"""
    payload = _session(thinking_budget_tokens=2048)._build_payload(
        ApiRequest(model="claude", messages=[ApiMessage(role="user", content="hi")]))
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert payload["max_tokens"] > 2048


def test_anthropic_reasoning_effort_none_disables_thinking() -> None:
    """请求级 reasoning_effort="none" 覆盖客户端预算 → 不开 thinking。"""
    payload = _session(thinking_budget_tokens=2048)._build_payload(ApiRequest(
        model="claude", reasoning_effort="none",
        messages=[ApiMessage(role="user", content="hi")]))
    assert "thinking" not in payload


@pytest.mark.parametrize(
    ("request_kwargs", "message"),
    [
        ({"temperature": 0.2}, "temperature"),
        ({"max_output_tokens": 1024}, "must exceed the thinking budget"),
    ],
)
def test_anthropic_thinking_conflicts_raise(request_kwargs: dict, message: str) -> None:
    """与 thinking 冲突的显式参数 → 报错而非静默改值 / 丢弃。"""
    with pytest.raises(InvalidRequestError, match=message):
        _session(thinking_budget_tokens=2048)._build_payload(ApiRequest(
            model="claude", messages=[ApiMessage(role="user", content="hi")], **request_kwargs))


def test_anthropic_budget_below_minimum_rejected_at_construction() -> None:
    """预算低于 1024 → 构造期报错。"""
    with pytest.raises(InvalidRequestError, match="1024"):
        AnthropicClient(api_key="k", thinking_budget_tokens=512)


async def test_engine_passes_signed_thinking_back_on_tool_continuation(
    monkeypatch: pytest.MonkeyPatch, skills_dir: Path, threads_dir: Path,
) -> None:
    """端到端：第二次请求的 assistant 消息以带签名的 thinking 块开头。"""
    requests: list[dict[str, Any]] = []
    bodies = [_thinking_tool_stream(), _text_stream("审查完成")]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=bodies[len(requests) - 1])

    _patch_httpx(monkeypatch, handler)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[],
        model_client=AnthropicClient(api_key="k", model="claude", thinking_budget_tokens=2048),
    )
    engine = await pool.get_or_create(session_id="think", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="审查代码"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed", ev.msg.data
            break

    assert len(requests) == 2
    assistant = next(m for m in requests[1]["messages"] if m["role"] == "assistant")
    assert assistant["content"][0] == {
        "type": "thinking", "thinking": "先读一下子 skill", "signature": "SIG-1"}
    assert assistant["content"][1] == {"type": "redacted_thinking", "data": "ENCRYPTED"}
    assert assistant["content"][-1]["type"] == "tool_use"
    reasoning = [it for it in engine.history_snapshot() if it.kind == "reasoning"]
    assert reasoning and reasoning[0].payload["provider_reasoning"]["anthropic"]["blocks"]
    await pool.close()


# ============================================================
# Gemini
# ============================================================


def _gemini_sse(chunks: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode()


async def test_gemini_thought_signature_captured_and_thoughts_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thought part → reasoning_delta；functionCall 的 thoughtSignature → extra_content。"""
    body = _gemini_sse([{
        "candidates": [{"content": {"parts": [
            {"text": "想一想", "thought": True},
            {"functionCall": {"name": "lookup", "args": {"q": 1}}, "thoughtSignature": "GSIG"},
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3},
    }])
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, content=body))
    session = GeminiSession(api_key="k", model="gemini", base_url="https://g",
                            cancel=CancellationToken())
    events = [ev async for ev in session.stream(ApiRequest(
        model="gemini", messages=[ApiMessage(role="user", content="hi")]))]

    assert [e.data["delta"] for e in events if e.kind == "reasoning_delta"] == ["想一想"]
    assert not [e for e in events if e.kind == "text_delta"]
    done = next(e for e in events if e.kind == "tool_call_done")
    assert done.data["extra_content"] == {"google": {"thought_signature": "GSIG"}}


def test_gemini_function_call_part_carries_signature_back() -> None:
    """回传：functionCall part 带回 thoughtSignature。"""
    req = ApiRequest(model="gemini", messages=[
        ApiMessage(role="user", content="hi"),
        ApiMessage(role="assistant", content="", tool_calls=[{
            "id": "fc_1", "function": {"name": "lookup", "arguments": '{"q": 1}'},
            "extra_content": {"google": {"thought_signature": "GSIG"}},
        }]),
    ])
    _, contents = _to_gemini_contents(req)
    part = contents[1]["parts"][0]
    assert part["thoughtSignature"] == "GSIG"
    assert part["functionCall"]["name"] == "lookup"


def test_gemini_thinking_config() -> None:
    """客户端预算 + include_thoughts → generationConfig.thinkingConfig；effort 覆盖预算。"""
    session = GeminiSession(api_key="k", model="gemini", base_url="https://g",
                            cancel=CancellationToken(), thinking_budget=4096,
                            include_thoughts=True)
    base = ApiRequest(model="gemini", messages=[ApiMessage(role="user", content="hi")])
    assert session._build_payload(base)["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 4096, "includeThoughts": True}
    low = base.model_copy(update={"reasoning_effort": "low"})
    assert session._build_payload(low)["generationConfig"]["thinkingConfig"][
        "thinkingBudget"] == 1024
