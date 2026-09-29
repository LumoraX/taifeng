"""journal-replay —— Responses 协议录制的确定性回放与 provider 回传状态还原（ADR 0070）。

录制：真实 ``OpenAIResponsesClient`` / ``AnthropicClient`` 经 ``httpx.MockTransport`` 在 strict
audit pool 里跑，Journal 记下请求安全投影、完整摘要与最终响应。回放：普通 pool（新 thread id、
新 submission id）挂 ``JournalReplayClient``，同样输入必须逐个消费全部录制调用。
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.anthropic_provider import AnthropicClient
from taifeng.llm.providers.openai.responses import OpenAIResponsesClient
from taifeng.llm.providers.replay import JournalReplayClient, recorded_calls
from taifeng.loop.audit_config import AuditConfig
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path

_PROMPTS = ("检查一下", "再看一遍")


def _responses_sse(response_id: str, output: list[dict[str, object]]) -> bytes:
    """只依赖 terminal truth 的 Responses SSE。"""
    event = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "model": "gpt-5.6-2026-08-01",
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        },
    }
    return f"data: {json.dumps(event)}\n\n".encode()


def _message(item_id: str, text: str) -> dict[str, object]:
    """一条 assistant message 输出项。"""
    return {
        "id": item_id, "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


def _responses_bodies() -> list[bytes]:
    """两轮对话：第一轮 reasoning(密文) + 工具调用 → 结论；第二轮直接回答。"""
    return [
        _responses_sse("resp-1", [
            {"id": "rs-1", "type": "reasoning", "encrypted_content": "ciphertext-1",
             "summary": [{"type": "summary_text", "text": "先读 skill"}], "status": "completed"},
            {"id": "fc-1", "type": "function_call", "call_id": "call-1", "name": "read_skill",
             "arguments": '{"skill_id":"style-checker"}', "status": "completed"},
        ]),
        _responses_sse("resp-2", [_message("msg-2", "第一轮结论")]),
        _responses_sse("resp-3", [_message("msg-3", "第二轮结论")]),
    ]


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, bodies: list[bytes]) -> None:
    """让所有 httpx.AsyncClient 按顺序返回脚本化响应。"""
    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(1)
        return httpx.Response(200, content=bodies[len(served) - 1])

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def _record(
    tmp_path: Path, skills_dir: Path, inner: Any, provider: str, model: str,
    prompts: tuple[str, ...],
) -> list[Any]:
    """在 strict audit pool 里跑完 prompts，返回 Journal 全部已提交记录。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "rec-threads",
        model_client=AttemptObservableClientAdapter(inner, provider=provider, default_model=model),
        compressors=[], audit=AuditConfig(
            journal_core=core, writer_id="w", max_attachment_bytes=65536,
            max_total_attachment_bytes=1048576))
    engine = await pool.get_or_create(session_id="ses-rec", entry_skill_id="code-reviewer")
    for prompt in prompts:
        op = taifeng.UserMessage(text=prompt)
        assert await run_until_root_done_kind(engine, op) == "turn_completed"
    await pool.close()
    return [record async for record in core.load("ses-rec")]


async def _replay(
    tmp_path: Path, skills_dir: Path, client: JournalReplayClient, prompts: tuple[str, ...],
) -> tuple[list[str], list[taifeng.ResponseItem]]:
    """普通 pool（新 thread / submission id）挂回放 client 跑同样输入。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "replay-threads",
        model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="ses-replay", entry_skill_id="code-reviewer")
    kinds = [
        await run_until_root_done_kind(engine, taifeng.UserMessage(text=prompt))
        for prompt in prompts
    ]
    history = engine.history_snapshot()
    await pool.close()
    return kinds, history


def _shape(items: list[taifeng.ResponseItem]) -> list[tuple[str, str]]:
    """history 的 (kind, 可比内容) 序列——id / 时间戳 / 采样 id 每次运行不同，不比。"""
    shape: list[tuple[str, str]] = []
    for item in items:
        payload = {k: v for k, v in item.payload.items() if k != "call_id"}
        shape.append((item.kind, json.dumps(payload, sort_keys=True, ensure_ascii=False)))
    return shape


async def _recorded_history(records: list[Any]) -> list[taifeng.ResponseItem]:
    """从录制 Journal 取 root thread 的会话项。"""
    from taifeng.conversation.journal.records import ConversationItemV1, deserialize_response_item

    root = records[1].thread_id
    return [
        deserialize_response_item(ConversationItemV1.model_validate(record.payload))
        for record in records
        if record.record_type == "conversation_item" and record.thread_id == root
    ]


@pytest.mark.asyncio
async def test_responses_replay_reproduces_recording_across_runs(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 thread / submission id 下同样输入：3 次录制调用全部按序消费，history 与录制一致，
    reasoning 的加密续传状态随录制还原。"""
    _patch_httpx(monkeypatch, _responses_bodies())
    inner = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    records = await _record(tmp_path, skills_dir, inner, "openai", "gpt-5.6", _PROMPTS)
    client = JournalReplayClient.from_records(records, capabilities=inner.capabilities)
    assert client.protocol == "responses"

    kinds, history = await _replay(tmp_path, skills_dir, client, _PROMPTS)

    assert kinds == ["turn_completed", "turn_completed"]
    assert client.remaining == 0
    assert len(client.consumed) == 3
    reasoning = next(item for item in history if item.kind == "reasoning")
    assert reasoning.payload["provider_state"]["payload"]["encrypted_content"] == "ciphertext-1"
    assert _shape(history) == _shape(await _recorded_history(records))
    # 回放 thread 的采样 id 由新运行派生，与录制不同——匹配靠一致重命名而非字面相等
    recorded = await _recorded_history(records)
    assert reasoning.metadata["llm_sample_id"] != next(
        item for item in recorded if item.kind == "reasoning"
    ).metadata["llm_sample_id"]


@pytest.mark.asyncio
async def test_responses_replay_detects_ciphertext_divergence(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """脱敏内容不同也是分叉：密文被换掉后第二次请求只在脱敏字段上不同，复核失败。"""
    _patch_httpx(monkeypatch, _responses_bodies())
    inner = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    records = await _record(tmp_path, skills_dir, inner, "openai", "gpt-5.6", _PROMPTS[:1])
    calls = recorded_calls(records)
    first = calls[0]
    tampered_items = json.loads(json.dumps(first.normalized_items))
    tampered_items[0]["state"]["payload"]["encrypted_content"] = "ciphertext-forged"
    calls[0] = dataclasses.replace(first, normalized_items=tuple(tampered_items))
    client = JournalReplayClient(calls, capabilities=inner.capabilities)

    kinds, _ = await _replay(tmp_path, skills_dir, client, _PROMPTS[:1])

    assert kinds == ["turn_failed"]
    assert client.consumed == [first.request_record_id]
    assert client.remaining == 1


@pytest.mark.asyncio
async def test_responses_replay_detects_prompt_divergence(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入不同 → 定位即失败 → 显式分叉（回归信号），不按顺序硬塞下一条录制。"""
    _patch_httpx(monkeypatch, _responses_bodies())
    inner = OpenAIResponsesClient(api_key="sk-test", model="gpt-5.6")
    records = await _record(tmp_path, skills_dir, inner, "openai", "gpt-5.6", _PROMPTS[:1])
    client = JournalReplayClient.from_records(records, capabilities=inner.capabilities)

    kinds, _ = await _replay(tmp_path, skills_dir, client, ("换一个问题",))

    assert kinds == ["turn_failed"]
    assert client.consumed == []


def _anthropic_sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    """Anthropic SSE 帧。"""
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _anthropic_bodies() -> list[bytes]:
    """thinking（含签名）+ tool_use → 文本结论。"""
    start = ("message_start", {"message": {"usage": {"input_tokens": 10, "output_tokens": 0}}})
    stop = ("message_stop", {"type": "message_stop"})
    return [
        _anthropic_sse([
            start,
            ("content_block_start", {"index": 0, "content_block": {
                "type": "thinking", "thinking": "", "signature": ""}}),
            ("content_block_delta", {"index": 0, "delta": {
                "type": "thinking_delta", "thinking": "先读子 skill"}}),
            ("content_block_delta", {"index": 0, "delta": {
                "type": "signature_delta", "signature": "SIG-1"}}),
            ("content_block_stop", {"index": 0}),
            ("content_block_start", {"index": 1, "content_block": {
                "type": "tool_use", "id": "tu_1", "name": "read_skill"}}),
            ("content_block_delta", {"index": 1, "delta": {
                "type": "input_json_delta", "partial_json": '{"skill_id": "style-checker"}'}}),
            ("content_block_stop", {"index": 1}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"},
                               "usage": {"output_tokens": 30}}),
            stop,
        ]),
        _anthropic_sse([
            start,
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "结论"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 3}}),
            stop,
        ]),
    ]


@pytest.mark.asyncio
async def test_chat_replay_restores_thinking_signature_passback(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat 协议：同 batch 会话项里的签名状态被还原，续传请求才能与录制逐字节一致。"""
    _patch_httpx(monkeypatch, _anthropic_bodies())
    inner = AnthropicClient(api_key="k", model="claude", thinking_budget_tokens=2048)
    records = await _record(tmp_path, skills_dir, inner, "anthropic", "claude", _PROMPTS[:1])
    client = JournalReplayClient.from_records(records)
    assert client.protocol == "chat"
    assert recorded_calls(records)[0].reasoning_state is not None

    kinds, history = await _replay(tmp_path, skills_dir, client, _PROMPTS[:1])

    assert kinds == ["turn_completed"]
    assert client.remaining == 0
    reasoning = next(item for item in history if item.kind == "reasoning")
    blocks = reasoning.payload["provider_reasoning"]["anthropic"]["blocks"]
    assert blocks[0]["signature"] == "SIG-1"
