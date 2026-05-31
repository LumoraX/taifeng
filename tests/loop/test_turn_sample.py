"""_sample_once 三段式回归：默认 max_parallel=1 时，一条消息多 tool call
的历史必须按 (function_call, function_call_output) 配对、按发起序写入。

这是并发改造的关键回归护栏：并发度=1 必须与历史 transcript 字节级等价。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import taifeng

if TYPE_CHECKING:
    from pathlib import Path
from taifeng.llm.providers import MockClient, MockTurn


@pytest.mark.asyncio
async def test_two_tool_calls_paired_in_emission_order(
    skills_dir: Path, threads_dir: Path
) -> None:
    """首轮吐两个 read_skill（c0/c1），默认串行：历史应是 c0 call→c0 out→c1 call→c1 out。"""
    client = MockClient(turns=[
        MockTurn(text="并行读两个 skill", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
        ]),
        MockTurn(text="读取完毕，给出结论。"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break

    # 读回持久化 thread，过滤出 function_call / function_call_output
    gen = await pool.store.load_thread(engine.thread_id)
    items = [it async for it in gen]
    pairs = [
        (it.kind, it.payload.get("call_id", ""))
        for it in items
        if it.kind in ("function_call", "function_call_output")
    ]
    await pool.close()

    assert pairs == [
        ("function_call", "c0"),
        ("function_call_output", "c0"),
        ("function_call", "c1"),
        ("function_call_output", "c1"),
    ]


@pytest.mark.asyncio
async def test_tool_batch_dispatched_event_emitted(
    skills_dir: Path, threads_dir: Path
) -> None:
    """有 tool call 的轮次应 emit tool_batch_dispatched，count=2、max_parallel=1。"""
    client = MockClient(turns=[
        MockTurn(text="读", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
        ]),
        MockTurn(text="完毕。"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    batch_events: list[dict] = []

    async def consume() -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "tool_batch_dispatched":
                batch_events.append(ev.msg.data)
            if ev.msg.kind == "shutdown":
                break

    import asyncio
    task = asyncio.create_task(consume())
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()

    assert len(batch_events) == 1
    assert batch_events[0] == {"count": 2, "max_parallel": 1}
