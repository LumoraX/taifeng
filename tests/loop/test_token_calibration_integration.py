"""token-accounting-calibration 端到端 —— provider 实测 usage 驱动上下文估算。

验证：
  - 采样成功后 ``engine.estimate_tokens()`` 用实测 prompt token（而非 len/3.5 粗估）；
  - 下一 turn 的 pre-turn 预算判定（budget hint）同样走实测口径；
  - 破 cache 的压缩使锚点失效，但 overhead 仍保留。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import CompactNow

if TYPE_CHECKING:
    from pathlib import Path


async def _run_turn(engine, text: str) -> list:
    """提交一条用户消息并收集到终态为止的事件。"""
    msgs: list = []
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        msgs.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    return msgs


async def test_estimate_tokens_uses_measured_prompt_after_sample(
    skills_dir: Path, threads_dir: Path
) -> None:
    """实测 prompt 5000 token → 估算至少 5000（粗估一条 "hi" 只有个位数）。"""
    client = SimClient(turns=[
        SimTurn(text="ok", usage=TokenUsage(input_tokens=5000, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client)
    engine = await pool.get_or_create(session_id="cal1", entry_skill_id="code-reviewer")

    assert engine.estimate_tokens() < 100
    await _run_turn(engine, "hi")

    estimate = engine.estimate_tokens()
    # 实测 5000 + 锚点后新增的 assistant 回复粗估（"ok" ≈ 1）
    assert 5000 <= estimate < 5050, estimate
    await pool.close()


async def test_pre_turn_budget_hint_uses_calibrated_estimate(
    skills_dir: Path, threads_dir: Path
) -> None:
    """粗估远低于 soft，但实测已过 soft → 第二个 turn 的 pre-turn 注预算提示。"""
    client = SimClient(turns=[
        SimTurn(text="ok", usage=TokenUsage(input_tokens=3000, output_tokens=5)),
        SimTurn(text="ok2", usage=TokenUsage(input_tokens=3100, output_tokens=5)),
    ])
    budget = ContextBudget(context_window=4000, soft_limit_ratio=0.5, hard_limit_ratio=0.95)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], budget=budget)
    engine = await pool.get_or_create(session_id="cal2", entry_skill_id="code-reviewer")

    first = await _run_turn(engine, "hi")
    assert not [m for m in first if m.kind == "budget_hint_injected"]

    second = await _run_turn(engine, "again")
    hints = [m for m in second if m.kind == "budget_hint_injected"]
    assert hints, [m.kind for m in second]
    assert hints[0].data["used"] >= 3000
    await pool.close()


async def test_cache_breaking_compaction_invalidates_anchor(
    skills_dir: Path, threads_dir: Path
) -> None:
    """handoff 压缩改写前缀 → 锚点失效，overhead 保留继续修正粗估。"""
    client = SimClient(turns=[
        SimTurn(text=f"r{i}", usage=TokenUsage(input_tokens=2000, output_tokens=5))
        for i in range(4)
    ])
    summary_client = SimClient(turns=[
        SimTurn(text="## 摘要", usage=TokenUsage(input_tokens=100, output_tokens=10))
        for _ in range(2)
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[HandoffCompactionStrategy(model_client=summary_client)])
    engine = await pool.get_or_create(session_id="cal3", entry_skill_id="code-reviewer")
    for text in ("a", "b", "c"):
        await _run_turn(engine, text)

    cal_before = engine._token_calibration  # noqa: SLF001
    assert cal_before is not None and cal_before.anchor_valid

    sub_id = await engine.submit(CompactNow(force=True))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("compaction_completed", "turn_failed"):
            assert ev.msg.data.get("success"), ev.msg.data
            break

    cal_after = engine._token_calibration  # noqa: SLF001
    assert cal_after is not None
    assert not cal_after.anchor_valid
    assert cal_after.overhead_tokens == cal_before.overhead_tokens
    await pool.close()
