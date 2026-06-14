"""审计可观测 层1 —— LLM request 全文留痕（LlmRequestRecorded 事件）。

覆盖：
- ``enable_request_capture=False``（默认）→ 不 emit ``llm_request_recorded``；
- ``enable_request_capture=True`` → 每次实发 request 前 emit 一条，data 含全文；
- 留痕在「发送 provider 之前」→ 即便后续失败也已留痕（此处只验证正常路径含全文）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage


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
        await asyncio.wait_for(task, timeout=2.0)
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
