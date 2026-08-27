"""Responses terminal normalized output 到原子 history/tool replay 的集成测试。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.events import completed, normalized_output
from taifeng.llm.types import ApiRequest, TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.events import ResponseEvent
    from taifeng.loop.cancellation import CancellationToken


_STATE = {
    "provider": "openai",
    "protocol": "responses",
    "item_type": "reasoning",
    "payload": {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "ciphertext",
        "summary": [{"type": "summary_text", "text": "读取 skill"}],
        "status": "completed",
    },
}


class _ResponsesSession:
    """按脚本发 terminal internal events 的无网络 session。"""

    def __init__(self, events: list[ResponseEvent]) -> None:
        self._events = events

    async def __aenter__(self) -> _ResponsesSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        """无资源。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """按顺序回放事件。"""
        for event in self._events:
            yield event


class _ResponsesClient:
    """捕获每轮请求的 Responses conformance client。"""

    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="openai",
        protocol="responses",
        accepts_provider_state=True,
    )

    def __init__(self, turns: list[list[ResponseEvent]]) -> None:
        self._turns = turns
        self.requests: list[ApiRequest] = []

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _ResponsesSession:
        """消费一轮脚本并包装请求捕获。"""
        events = self._turns.pop(0)
        outer = self

        class _CapturingSession(_ResponsesSession):
            async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
                outer.requests.append(request)
                async for event in super().stream(request):
                    yield event

        return _CapturingSession(events)


def _done(items: list[dict[str, object]], *, end_turn: bool) -> list[ResponseEvent]:
    """构造一轮成功的 terminal internal/public 事件。"""
    return [
        normalized_output(items),
        completed(response_id="resp", usage=TokenUsage(), end_turn=end_turn),
    ]


@pytest.mark.asyncio
async def test_responses_terminal_groups_are_atomic_and_replayed_in_order(
    skills_dir: Path, threads_dir: Path
) -> None:
    """reasoning/call 原子提交，tool output 带 origin，下一轮按 Item 顺序重放。"""
    sample_one = [
        {
            "type": "reasoning",
            "output_index": 0,
            "visible_text": "读取 skill",
            "state": _STATE,
        },
        {
            "type": "function_call",
            "output_index": 1,
            "call_id": "call-1",
            "name": "read_skill",
            "arguments": '{"skill_id":"style-checker"}',
        },
    ]
    sample_two = [
        {"type": "message", "output_index": 0, "text": "已完成图片检查。"}
    ]
    client = _ResponsesClient(
        [_done(sample_one, end_turn=False), _done(sample_two, end_turn=True)]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="responses-durable", entry_skill_id="code-reviewer"
    )
    submission_id = await engine.submit(taifeng.UserMessage(text="检查"))
    public_kinds: list[str] = []
    async for event in engine.subscribe(submission_id):
        public_kinds.append(event.msg.kind)
        if event.msg.kind in {"turn_completed", "turn_failed"}:
            assert event.msg.kind == "turn_completed"
            break

    stream = await pool.store.load_thread(engine.thread_id)
    items = [item async for item in stream]
    await pool.close()

    kinds = [item.kind for item in items]
    assert kinds == [
        "user_message",
        "reasoning",
        "function_call",
        "function_call_output",
        "assistant_message",
    ]
    first_sample = items[1].metadata["llm_sample_id"]
    assert items[2].metadata["llm_sample_id"] == first_sample
    assert items[3].metadata["origin_llm_sample_id"] == first_sample
    assert items[1].payload["provider_state"] == _STATE
    assert "normalized_output" not in public_kinds
    second_input_types = [item.type for item in client.requests[1].input_items]
    assert second_input_types[-3:] == [
        "provider_state",
        "function_call",
        "function_call_output",
    ]
    raw_lines = (threads_dir / f"{engine.thread_id}.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    frames = [json.loads(line).get("frame") for line in raw_lines]
    assert frames.count("item_batch_commit") == 2
