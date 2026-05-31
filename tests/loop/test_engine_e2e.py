"""端到端：Pool → Engine → 一次 turn (含 tool call) → 持久化验证。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage


@pytest.mark.asyncio
async def test_pool_engine_basic_turn(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[
        MockTurn(text="分析中...", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'}
        ], usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120)),
        MockTurn(text="最终结论：右上叶 8mm 结节。", usage=TokenUsage(input_tokens=200, output_tokens=30, total_tokens=230)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    events_seen: list[str] = []

    async def consume():
        async for ev in engine.subscribe_all():
            events_seen.append(ev.msg.kind)
            if ev.msg.kind in ("turn_completed", "turn_failed", "shutdown"):
                if ev.msg.kind != "shutdown":
                    break
        return None

    task = asyncio.create_task(consume())
    sub_id = await engine.submit(taifeng.UserMessage(text="请分析 CT"))

    # 等待 turn 完成
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            outcome_kind = ev.msg.kind
            break

    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()

    assert outcome_kind == "turn_completed"
    assert "tool_call_started" in events_seen
    assert "tool_call_completed" in events_seen


@pytest.mark.asyncio
async def test_engine_persists_thread(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[MockTurn(text="ok", usage=TokenUsage(input_tokens=50, output_tokens=5))])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="hello"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    # 验证 store
    tid = engine.thread_id
    gen = await pool.store.load_thread(tid)
    items = [it async for it in gen]
    kinds = [it.kind for it in items]
    assert "user_message" in kinds
    assert "assistant_message" in kinds

    await pool.close()


@pytest.mark.asyncio
async def test_entry_skill_validation(skills_dir: Path, threads_dir: Path) -> None:
    """atomic skill 不允许作为 entry。"""
    client = MockClient(turns=[])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    with pytest.raises(ValueError):
        await pool.get_or_create(session_id="s1", entry_skill_id="style-checker")
    await pool.close()


@pytest.mark.asyncio
async def test_unknown_skill_rejected(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    with pytest.raises(ValueError):
        await pool.get_or_create(session_id="s1", entry_skill_id="no-such-skill")
    await pool.close()


@pytest.mark.asyncio
async def test_engine_run_script_e2e(tmp_path: Path) -> None:
    """LLM 调用 run_script → script 实际执行 → 结果回流到 LLM。

    搭一个最小 skill 含 1 个 shell script，MockClient 第一轮调 run_script，
    第二轮返回最终 text。验证 ScriptExecutor 被 EnginePool 注入并执行。
    """
    from taifeng.skill.scripts.shell import ShellScriptExecutor

    skills = tmp_path / "skills"
    helper_dir = skills / "helper"
    helper_dir.mkdir(parents=True)
    (helper_dir / "SKILL.md").write_text(
        """---
name: helper
description: 占位辅助
version: 1.0.0
type: atomic
---
# helper body
""",
        encoding="utf-8",
    )

    skill_dir = skills / "data-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: data-skill
description: 数据 skill
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [helper]
tool_names: []
scripts:
  - name: greet
    path: scripts/greet.sh
    language: shell
    timeout_seconds: 5
    description: 打个招呼
---
# data-skill body
""",
        encoding="utf-8",
    )
    script = skill_dir / "scripts" / "greet.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hello-from-greet\n", encoding="utf-8")
    script.chmod(0o755)

    threads = tmp_path / "threads"
    threads.mkdir()

    client = MockClient(turns=[
        MockTurn(
            text="先跑 script",
            tool_calls=[{
                "id": "c1",
                "name": "run_script",
                "arguments": (
                    '{"skill_id": "data-skill", '
                    '"script_name": "greet", "args": {}}'
                ),
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="script 输出: hello-from-greet",
            usage=TokenUsage(input_tokens=20, output_tokens=5, total_tokens=25),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=threads,
        model_client=client,
        compressors=[],
        script_executors={"shell": ShellScriptExecutor()},
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="data-skill"
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="run greet"))

    outcome_kind = None
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            outcome_kind = ev.msg.kind
            break

    await pool.close()
    assert outcome_kind == "turn_completed"

    # 验证 store 中能看到 run_script 的 tool_call_output
    items = [
        it async for it in await pool.store.load_thread(engine.thread_id)
    ]
    tool_outputs = [it for it in items if it.kind == "function_call_output"]
    assert tool_outputs, "expect at least one function_call_output"
    assert any(
        "hello-from-greet" in (it.payload.get("output") or "")
        for it in tool_outputs
    )
