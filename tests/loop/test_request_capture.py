"""审计可观测 层1 —— LLM request 全文留痕（LlmRequestRecorded 事件）。

覆盖：
- ``enable_request_capture=False``（默认）→ 不 emit ``llm_request_recorded``；
- ``enable_request_capture=True`` → 每次实发 request 前 emit 一条，data 含全文；
- 留痕在「发送 provider 之前」→ 即便后续失败也已留痕（此处只验证正常路径含全文）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path


async def _run_turn_collect(
    skills_dir: Path, threads_dir: Path, *, capture: bool
) -> list[taifeng.EventMsg]:
    """跑一次 turn，收集 firehose 全部事件返回。"""
    client = SimClient(
        turns=[SimTurn(text="ok", usage=TokenUsage(input_tokens=5, output_tokens=2))]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        enable_request_capture=capture,
    )
    engine = await pool.get_or_create(
        session_id="s_cap", entry_skill_id="code-reviewer"
    )

    collected: list[taifeng.EventMsg] = []

    async def consume() -> None:
        async for ev in engine.subscribe_all():
            collected.append(ev)
            if ev.msg.kind == "shutdown":
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让 consume 注册 firehose 队列
    sub_id = await engine.submit(taifeng.UserMessage(text="hi"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        task.cancel()
    return collected


@pytest.mark.asyncio
async def test_request_not_captured_by_default(
    skills_dir: Path, threads_dir: Path
) -> None:
    """默认关 → 不 emit llm_request_recorded（零泄漏面）。"""
    events = await _run_turn_collect(skills_dir, threads_dir, capture=False)
    assert not [e for e in events if e.msg.kind == "llm_request_recorded"]


@pytest.mark.asyncio
async def test_request_captured_with_full_body_when_enabled(
    skills_dir: Path, threads_dir: Path
) -> None:
    """开启 → emit llm_request_recorded，data 为 ApiRequest 全文（含 model/messages）。"""
    events = await _run_turn_collect(skills_dir, threads_dir, capture=True)
    recorded = [e for e in events if e.msg.kind == "llm_request_recorded"]
    assert len(recorded) >= 1
    data = recorded[0].msg.data
    assert "model" in data
    assert "messages" in data  # request 全文（ApiRequest.model_dump）


@pytest.mark.asyncio
async def test_request_captured_in_call_skill_sub_runner(
    skills_dir: Path, threads_dir: Path
) -> None:
    """call_skill 同步派发的子 TurnRunner 也必须继承捕获开关。

    回归：turn_dispatch.spawn_sub_runner 曾漏传 enable_request_capture，子步骤的
    request 从不留痕；声明式编排入口无 LLM 迭代时，整条链的捕获会变成零。
    """
    client = SimClient(
        turns=[
            SimTurn(
                text="派发",
                tool_calls=[
                    {
                        "id": "c0",
                        "name": "call_skill",
                        "arguments": '{"skill_id":"style-checker","args":{"x":1},"reason":"审查风格"}',
                    }
                ],
            ),
            SimTurn(text="子步骤完成"),
            SimTurn(text="收尾"),
        ]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        enable_request_capture=True,
    )
    engine = await pool.get_or_create(
        session_id="s_cap_sub", entry_skill_id="code-reviewer"
    )

    collected: list[taifeng.EventMsg] = []
    root_done = asyncio.Event()

    async def consume() -> None:
        # 子 turn 与根 turn 共用 submission_id，按 submission 订阅会在子 turn_completed
        # 处提前结束；故走 firehose，以「skill_returned 之后的首个终态」判定根 turn 结束
        child_returned = False
        async for ev in engine.subscribe_all():
            collected.append(ev)
            if ev.msg.kind == "skill_returned":
                child_returned = True
            elif child_returned and ev.msg.kind in ("turn_completed", "turn_failed"):
                root_done.set()
            if ev.msg.kind == "shutdown":
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让 consume 注册 firehose 队列
    await engine.submit(taifeng.UserMessage(text="hi"))
    await asyncio.wait_for(root_done.wait(), timeout=GUARD_TIMEOUT_SECONDS)
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        task.cancel()

    kinds = [e.msg.kind for e in collected]
    # 子 turn 区间 = 第二个 turn_started（子）到 skill_returned 之间
    child_start = [i for i, k in enumerate(kinds) if k == "turn_started"][1]
    child_end = kinds.index("skill_returned")
    child_captured = [
        k for k in kinds[child_start:child_end] if k == "llm_request_recorded"
    ]
    assert len(child_captured) == 1  # 子步骤那一次采样必须留痕
    # 整条链三次采样（入口派发 / 子步骤 / 入口收尾）全部留痕
    assert kinds.count("llm_request_recorded") == 3
