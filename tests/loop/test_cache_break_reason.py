"""G-CACHE：cache 失效原因自动归因（结构指纹）+ 跨 turn 持久化。

- 单元层：_compute_prompt_fingerprint / _detect_structural_break_reason 的归因逻辑
- 引擎层：cache_stats 跨 turn 持久，drop 被检出（此前每 turn 重置 → 检不出）
"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _Stub:
    """轻量带属性占位 —— 模拟 ToolSpecRef(.name) / ResolvedInstruction(.text)。"""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


async def _make_runner(skills_dir: Path) -> TurnRunner:
    """构造一个仅用于调用指纹方法的最小 TurnRunner（不 run）。"""
    registry = await FilesystemSkillRegistry.load(skills_dir)
    snap = registry.snapshot()
    entry = registry.get("code-reviewer")
    assert entry is not None

    async def _emit(_: object) -> None:
        return None

    return TurnRunner(
        entry_skill=entry,
        snapshot=snap,
        model_client=SimClient(turns=[]),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=None,  # 指纹方法不触碰 store
        compressors=None,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
    )


@pytest.mark.asyncio
async def test_fingerprint_stable_no_change(skills_dir: Path) -> None:
    """结构不变 → 同一指纹 → 不归因任何结构性破坏。"""
    runner = await _make_runner(skills_dir)
    tools = [_Stub(name="read_skill"), _Stub(name="call_skill")]
    fp1 = runner._compute_prompt_fingerprint(tools)  # noqa: SLF001
    runner.last_prompt_fingerprint = fp1
    assert runner._detect_structural_break_reason(fp1) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_fingerprint_detects_tool_change(skills_dir: Path) -> None:
    """tool 集合变化 → 归因 tool_spec_changed。"""
    runner = await _make_runner(skills_dir)
    fp_a = runner._compute_prompt_fingerprint([_Stub(name="a")])  # noqa: SLF001
    runner.last_prompt_fingerprint = fp_a
    fp_b = runner._compute_prompt_fingerprint(  # noqa: SLF001
        [_Stub(name="a"), _Stub(name="b")]
    )
    assert runner._detect_structural_break_reason(fp_b) == "tool_spec_changed"  # noqa: SLF001


@pytest.mark.asyncio
async def test_fingerprint_detects_instruction_change(skills_dir: Path) -> None:
    """注入指令文本变化 → 归因 system_prompt_changed。"""
    runner = await _make_runner(skills_dir)
    tools = [_Stub(name="read_skill")]
    fp_a = runner._compute_prompt_fingerprint(tools)  # noqa: SLF001
    runner.last_prompt_fingerprint = fp_a
    runner.instructions = [_Stub(text="新的系统级指令")]
    fp_b = runner._compute_prompt_fingerprint(tools)  # noqa: SLF001
    reason = runner._detect_structural_break_reason(fp_b)  # noqa: SLF001
    assert reason == "system_prompt_changed"


@pytest.mark.asyncio
async def test_engine_cache_stats_persist_and_detect_break_across_turns(
    skills_dir: Path, threads_dir: Path
) -> None:
    """跨 turn：turn2 的 cache_read 跌破 turn1 → 检出 break（无结构变更→非预期）。

    回归点：此前 cache_stats 每 turn 新建，last_cache_read 重置为 None，
    turn 间的 drop 永远检不出。现 engine 持久化 → 必能检出。
    """
    client = SimClient(turns=[
        SimTurn(text="一", cache_read=100, usage=TokenUsage(input_tokens=100)),
        SimTurn(text="二", cache_read=10, usage=TokenUsage(input_tokens=100)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer"
    )

    for text in ("第一轮", "第二轮"):
        sub_id = await engine.submit(taifeng.UserMessage(text=text))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

    stats = engine.cache_stats
    assert stats.last_cache_read_input_tokens == 10
    # 无结构变更 → 该 break 记为非预期（结构指纹两轮一致）
    assert stats.unexpected_cache_breaks == 1
    assert stats.last_break is not None
    assert stats.last_break.token_drop == 90
    assert stats.last_break.reason == "unknown_drop"

    await pool.close()
