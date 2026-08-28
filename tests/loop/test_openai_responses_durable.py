"""Responses terminal normalized output 到原子 history/tool replay 的集成测试。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.events import completed, normalized_output
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.submission import Resume, Rewind
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
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


async def _wait_until(predicate: Callable[[], bool], *, attempts: int = 300) -> bool:
    """短轮询 detached spawn 终态，避免测试依赖固定长等待。"""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _write_spawn_skills(root: Path) -> Path:
    """写入可直接 spawn 的最小 host/worker skill 集。"""
    skills = root / "responses-spawn-skills"
    definitions = {
        "host": """---
name: host
description: Responses spawn 测试入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker]
max_call_depth: 3
---
# Host
""",
        "worker": """---
name: worker
description: Responses spawn 测试子任务
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# Worker
""",
    }
    for name, body in definitions.items():
        skill_dir = skills / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


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
    assert first_sample == f"{engine.thread_id}:{submission_id}:turn:0:llm:1"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [normalized_output([{"type": "message", "output_index": 0, "text": "x"}])],
        [
            completed(response_id="resp", usage=TokenUsage(), end_turn=True),
            normalized_output([{"type": "message", "output_index": 0, "text": "x"}]),
        ],
        [
            normalized_output([{"type": "message", "output_index": 0, "text": "x"}]),
            completed(response_id="resp", usage=TokenUsage(), end_turn=True),
            completed(response_id="resp", usage=TokenUsage(), end_turn=True),
        ],
    ],
    ids=["missing-completed", "normalized-after-completed", "duplicate-completed"],
)
async def test_invalid_responses_terminal_sequence_is_never_committed(
    skills_dir: Path,
    threads_dir: Path,
    events: list[ResponseEvent],
) -> None:
    """Responses 只接受 normalized_output 后紧随唯一 completed 的终态。"""
    client = _ResponsesClient([events])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="responses-invalid-terminal", entry_skill_id="code-reviewer"
    )

    submission_id = await engine.submit(taifeng.UserMessage(text="检查"))
    terminal = None
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in {"turn_completed", "turn_failed"}:
            terminal = event.msg
            break

    stream = await pool.store.load_thread(engine.thread_id)
    items = [item async for item in stream]
    await pool.close()

    assert terminal is not None
    assert terminal.kind == "turn_failed"
    assert [item.kind for item in items] == ["user_message"]


@pytest.mark.asyncio
async def test_responses_spawn_rewind_uses_a_new_logical_sample_scope(
    tmp_path: Path, threads_dir: Path
) -> None:
    """同一 child thread 的主动 rewind 是新采样，不得撞首轮 batch id。"""
    client = _ResponsesClient([
        _done([{"type": "message", "output_index": 0, "text": "首次完成"}],
              end_turn=True),
        _done([{"type": "message", "output_index": 0, "text": "重推完成"}],
              end_turn=True),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_spawn_skills(tmp_path),
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="responses-spawn-rewind", entry_skill_id="host"
    )
    spawned = await engine.spawn_skill(skill_id="worker", args={}, reason="test")
    handle_id = spawned["handle_id"]
    child_thread_id = spawned["child_thread_id"]
    assert await _wait_until(
        lambda: engine.spawn_status([handle_id])[handle_id]["status"] == "done"
    )

    nodes = await engine.rewind_nodes_for(child_thread_id)
    await engine.submit(Rewind(
        node_id=nodes[0].node_id,
        thread_id=child_thread_id,
        mode="re_reason",
    ))

    assert await _wait_until(
        lambda: engine.spawn_status([handle_id])[handle_id]["result"] == "重推完成"
    )
    await pool.close()


@pytest.mark.asyncio
async def test_responses_spawn_resume_uses_a_new_logical_sample_scope(
    tmp_path: Path, threads_dir: Path
) -> None:
    """同一 child thread 的 HITL resume 是新采样，不得撞挂起前 batch id。"""
    client = _ResponsesClient([
        _done([
            {
                "type": "function_call",
                "output_index": 0,
                "call_id": "question-1",
                "name": "request_user_input",
                "arguments": '{"prompt":"请补充"}',
            }
        ], end_turn=False),
        _done([{"type": "message", "output_index": 0, "text": "续跑完成"}],
              end_turn=True),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_spawn_skills(tmp_path),
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(
        session_id="responses-spawn-resume", entry_skill_id="host"
    )
    events = []

    async def collect_events() -> None:
        """侧录 spawn 挂起事件以取得待核销 request id。"""
        async for event in engine.subscribe_all():
            events.append(event)

    collector = asyncio.create_task(collect_events())
    await asyncio.sleep(0)
    spawned = await engine.spawn_skill(skill_id="worker", args={}, reason="test")
    handle_id = spawned["handle_id"]
    child_thread_id = spawned["child_thread_id"]
    assert await _wait_until(
        lambda: engine.spawn_status([handle_id])[handle_id]["status"] == "suspended"
    )
    suspended = next(event for event in events if event.msg.kind == "spawn_suspended")
    request_id = suspended.msg.data["pending"][0]["request_id"]

    await engine.submit(Resume(
        thread_id=child_thread_id,
        resolutions={request_id: {"answer": "已补充"}},
    ))

    assert await _wait_until(
        lambda: engine.spawn_status([handle_id])[handle_id]["result"] == "续跑完成"
    )
    collector.cancel()
    with suppress(asyncio.CancelledError):
        await collector
    await pool.close()
