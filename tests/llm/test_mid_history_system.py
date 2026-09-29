"""mid-history-system —— 历史中段 system 消息在原生 provider 上不得丢失。

回归点：Anthropic / Gemini 原生 API 只有顶层 system 字段，此前两家 provider 直接
``continue`` 掉 messages 里的 system 消息——压缩摘要、pinned 重注、预算提示、记忆预取、
业务注入全部静默消失，一压缩就等于把被压缩的历史整段删掉。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import taifeng
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers.anthropic_provider import AnthropicClient, _to_anthropic_messages
from taifeng.llm.providers.gemini_provider import _to_gemini_contents
from taifeng.llm.providers.openai_compat import OpenAICompatSession
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.types import ApiMessage, ApiMessageItem, ApiRequest, TextPart
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CompactNow

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SUMMARY = "[Compacted history summary] 用户要求把报告写成三段；已完成第一段"


def _request(*messages: ApiMessage) -> ApiRequest:
    return ApiRequest(model="m", system_prompt=["SYS"], messages=list(messages))


def _anthropic_texts(req: ApiRequest) -> list[str]:
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    return [b.get("text", "") for m in msgs for b in m["content"] if b.get("type") == "text"]


def _gemini_texts(req: ApiRequest) -> list[str]:
    _, contents = _to_gemini_contents(req)
    return [p.get("text", "") for c in contents for p in c["parts"] if "text" in p]


def _codex_texts(req: ApiRequest) -> list[str]:
    from taifeng.llm.providers.codex.wire import build_codex_payload

    ordered = ApiRequest(model="m", system_prompt=req.system_prompt,
                         input_items=[ApiMessageItem(role=m.role, content=m.content)
                                      for m in req.messages])
    payload = build_codex_payload(ordered, default_model="m")
    return [part["text"] for item in payload["input"] for part in item.get("content", [])
            if isinstance(part, dict) and "text" in part]


def _openai_texts(req: ApiRequest) -> list[str]:
    session = OpenAICompatSession(base_url="https://x", api_key="k", model="m",
                                  cancel=CancellationToken())
    payload = session._build_payload(req)  # noqa: SLF001
    return [str(m.get("content", "")) for m in payload["messages"]]


@pytest.mark.parametrize("texts_of", [_anthropic_texts, _gemini_texts, _openai_texts, _codex_texts],
                         ids=["anthropic", "gemini", "openai_compat", "codex"])
def test_mid_history_system_text_reaches_wire_on_every_provider(
    texts_of: Callable[[ApiRequest], list[str]],
) -> None:
    """一致性：任何原生 provider 都不得丢掉历史中段 system 消息的文本。"""
    req = _request(
        ApiMessage(role="system", content=SUMMARY),
        ApiMessage(role="user", content="继续"),
    )
    assert any(SUMMARY in text for text in texts_of(req))


def test_anthropic_system_note_becomes_tagged_user_block_in_place() -> None:
    """Anthropic：中段 system → 带标签 user 文本块，原位保留并与相邻 user 合并。"""
    req = _request(
        ApiMessage(role="user", content="写报告"),
        ApiMessage(role="assistant", content="好的"),
        ApiMessage(role="system", content=SUMMARY),
        ApiMessage(role="user", content="继续"),
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    tail = msgs[-1]["content"]
    assert tail[0]["text"].startswith("<system-reminder>") and SUMMARY in tail[0]["text"]
    assert tail[1]["text"] == "继续"


def test_anthropic_note_after_tool_result_keeps_tool_result_first() -> None:
    """工具结果之后的注记并入同一 user 消息，tool_result 仍在最前（Anthropic 要求）。"""
    req = _request(
        ApiMessage(role="user", content="查天气"),
        ApiMessage(role="assistant", content="",
                   tool_calls=[{"id": "t1", "function": {"name": "w", "arguments": "{}"}}]),
        ApiMessage(role="tool", content="晴", tool_call_id="t1"),
        ApiMessage(role="system", content="Context budget: ~90% used"),
    )
    _, msgs = _to_anthropic_messages(req, cache_indexes=set())
    last = msgs[-1]
    assert last["role"] == "user"
    assert [b["type"] for b in last["content"]] == ["tool_result", "text"]


def test_gemini_system_note_merges_with_adjacent_user_content() -> None:
    """Gemini：注记与前后 user 合并为一条 content，不产生连续同角色 content。"""
    req = _request(
        ApiMessage(role="user", content="写报告"),
        ApiMessage(role="system", content=SUMMARY),
        ApiMessage(role="user", content="继续"),
    )
    _, contents = _to_gemini_contents(req)
    assert [c["role"] for c in contents] == ["user"]
    texts = [p["text"] for p in contents[0]["parts"]]
    assert texts[0] == "写报告" and SUMMARY in texts[1] and texts[2] == "继续"


def test_system_note_with_part_list_content_keeps_text() -> None:
    """part 列表形态的 system 内容同样保留文本。"""
    req = _request(ApiMessage(role="system", content=[TextPart(text=SUMMARY)]))
    assert any(SUMMARY in t for t in _anthropic_texts(req))
    assert any(SUMMARY in t for t in _gemini_texts(req))


def _anthropic_text_stream(text: str) -> bytes:
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


async def test_anthropic_request_after_compaction_still_carries_summary(
    monkeypatch: pytest.MonkeyPatch, skills_dir: Path, threads_dir: Path,
) -> None:
    """端到端：handoff 压缩后，下一次发往 Anthropic 的请求里仍有压缩摘要。"""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=_anthropic_text_stream(f"回答{len(requests)}"))

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    summary_client = SimClient(turns=[SimTurn(text="## 摘要\n用户要求三段式报告 SUMMARY_MARK")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=AnthropicClient(api_key="k", model="claude"),
        compressors=[HandoffCompactionStrategy(model_client=summary_client)])
    engine = await pool.get_or_create(session_id="mid", entry_skill_id="code-reviewer")

    async def _turn(text: str) -> None:
        sub_id = await engine.submit(taifeng.UserMessage(text=text))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed", ev.msg.data
                break

    for text in ("第一段", "第二段", "第三段"):
        await _turn(text)
    sub_id = await engine.submit(CompactNow(force=True))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("compaction_completed", "turn_failed"):
            assert ev.msg.data.get("success"), ev.msg.data
            break
    await _turn("继续")

    wire = json.dumps(requests[-1]["messages"], ensure_ascii=False)
    assert "SUMMARY_MARK" in wire
    assert "<system-reminder>" in wire
    await pool.close()
