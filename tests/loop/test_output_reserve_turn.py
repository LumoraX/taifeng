"""skill ``max_output_tokens`` 联动输出预留（ADR 0071）的端到端测试。

回归点：skill 声明了较大的 ``inference.max_output_tokens`` 时，soft / hard 阈值仍只按
``ContextBudget.output_reserve_tokens`` 计算 → 压缩不及时、预算提示与发送前 hard 预检失真。
现在本 turn 生效预留 = max(预算预留, entry skill 声明)，覆盖：

- pre-turn 压缩触发与交给策略的 ``CompressionContext.budget``；
- 预算提示（``budget_hint_injected``）与发送前 hard 预检（``context_budget_exceeded``）；
- call_skill 子 turn 与 detached spawn 子 turn 各用各自 entry skill 的声明；
- 预留不小于窗口 → turn 显式失败且不发请求；
- ``CompactNow(target_tokens=...)`` 的 soft_limit 仍恰为 target。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.llm.providers.sim import SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import CompactNow
from tests.conftest import ATOMIC_SKILL, run_until_root_done, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.context.compressor import CompressionContext, CompressionResult
    from taifeng.context.injection import InitialContextInjection


class _RecordingStrategy:
    """只记录不压缩的策略：记下每次被咨询时的 (phase, 生效预留, soft_limit)。"""

    name = "recording"
    priority = 1

    def __init__(self) -> None:
        self.seen: list[tuple[str, int, int]] = []

    def should_trigger(self, ctx: CompressionContext) -> None:
        self.seen.append((ctx.phase, ctx.budget.output_reserve_tokens, ctx.budget.soft_limit))

    async def compress(
        self, ctx: CompressionContext, injection: InitialContextInjection,
    ) -> CompressionResult:
        raise AssertionError("recording strategy never triggers")


def _skills(root: Path, *, entry_max: int | None = None, child_max: int | None = None) -> Path:
    """composite entry（code-reviewer）+ atomic 子 skill（style-checker），按需声明输出上限。"""

    def block(value: int | None) -> str:
        return f"inference:\n  max_output_tokens: {value}\n" if value is not None else ""

    skills = root / "skills"
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(
        "---\nname: code-reviewer\ndescription: 代码审查专家\ntype: composite\nentry: true\n"
        f"child_skills: [style-checker]\n{block(entry_max)}---\n# 代码审查专家\n",
        encoding="utf-8",
    )
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(
        ATOMIC_SKILL.replace("type: atomic\n", f"type: atomic\n{block(child_max)}", 1),
        encoding="utf-8",
    )
    return skills


def _kinds(events: list[Any], kind: str) -> list[Any]:
    """挑出指定 kind 的事件消息。"""
    return [ev.msg for ev in events if ev.msg.kind == kind]


@pytest.mark.parametrize("declared", [None, 2_000])
async def test_declared_output_cap_tightens_soft_and_hard(
    tmp_path: Path, threads_dir: Path, sim_client: Any, declared: int | None,
) -> None:
    """窗口 4000、soft 0.5、hard 0.95，实测 prompt 1950：
    未声明 → soft 2000 / hard 3800 都未越过；声明 2000 → 生效 soft 1000 / hard 1900 双双越过。"""
    client = sim_client(turns=[
        SimTurn(text="ok", usage=TokenUsage(input_tokens=1950, output_tokens=5)),
        SimTurn(text="ok2", usage=TokenUsage(input_tokens=1990, output_tokens=5)),
    ])
    strategy = _RecordingStrategy()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path, entry_max=declared), threads_dir=threads_dir,
        model_client=client, compressors=[strategy],
        budget=ContextBudget(context_window=4000, soft_limit_ratio=0.5, hard_limit_ratio=0.95),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        await run_until_root_done(engine, taifeng.UserMessage(text="hi"))
        second = await run_until_root_done(engine, taifeng.UserMessage(text="again"))
    finally:
        await pool.close()

    hints = _kinds(second, "budget_hint_injected")
    exceeded = _kinds(second, "context_budget_exceeded")
    if declared is None:
        assert strategy.seen == []  # 从未越过 soft，策略不被咨询
        assert hints == [] and exceeded == []
        return
    assert ("pre_turn", 2_000, 1_000) in strategy.seen
    [hint] = hints
    assert hint.data["remaining_to_hard"] == max(0, 1_900 - hint.data["used"])
    assert exceeded and exceeded[0].data["hard_limit"] == 1_900


@pytest.mark.parametrize(("entry_max", "child_max"), [(None, 3_000), (3_000, None)])
async def test_call_skill_child_uses_its_own_declaration(
    tmp_path: Path, threads_dir: Path, sim_client: Any,
    entry_max: int | None, child_max: int | None,
) -> None:
    """父子各按自己的声明预留：子不继承父的生效预留，父也不沾子的。"""
    client = sim_client(turns=[
        SimTurn(text="派发", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "查风格"}',
        }]),
        SimTurn(text="风格无问题"),
        SimTurn(text="综合：通过"),
    ])
    strategy = _RecordingStrategy()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path, entry_max=entry_max, child_max=child_max),
        threads_dir=threads_dir, model_client=client, compressors=[strategy],
        # soft 比例极小：每次判定都越过 soft，策略每次都被咨询，便于观察生效预留
        budget=ContextBudget(context_window=4000, soft_limit_ratio=0.0001),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        events = await run_until_root_done(engine, taifeng.UserMessage(text="审查"))
    finally:
        await pool.close()

    assert _kinds(events, "turn_failed") == []
    parent, child = entry_max or 0, child_max or 0
    reserves = [reserve for _, reserve, _ in strategy.seen]
    assert reserves[0] == parent  # 父 pre_turn
    assert child in reserves  # 子 turn
    assert reserves[-1] == parent  # 父收尾


async def test_detached_spawn_child_uses_its_own_declaration(
    tmp_path: Path, threads_dir: Path, sim_client: Any,
) -> None:
    """spawn 出的子 thread 由子 skill 作 entry：判定用子 skill 的声明。"""
    client = sim_client(turns=[SimTurn(text="风格结论")])
    strategy = _RecordingStrategy()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path, child_max=3_000), threads_dir=threads_dir,
        model_client=client, compressors=[strategy],
        budget=ContextBudget(context_window=4000, soft_limit_ratio=0.0001),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        out = await engine.spawn_skill(skill_id="style-checker", args={}, reason="并发分析")
        handles = engine._spawn_handles  # noqa: SLF001
        await wait_for_condition(lambda: handles.is_terminal(out["handle_id"]))
    finally:
        await pool.close()

    assert strategy.seen and {reserve for _, reserve, _ in strategy.seen} == {3_000}


async def test_reserve_not_below_window_fails_turn_explicitly(
    tmp_path: Path, threads_dir: Path, sim_client: Any,
) -> None:
    """声明值 >= 窗口：turn 在首次预算判定处显式失败，不发任何请求。"""
    client = sim_client(turns=[])
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path, entry_max=4_000), threads_dir=threads_dir,
        model_client=client, compressors=[], budget=ContextBudget(context_window=4000),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        events = await run_until_root_done(engine, taifeng.UserMessage(text="hi"))
    finally:
        await pool.close()

    [failed] = _kinds(events, "turn_failed")
    assert failed.data["kind"] == "OutputReserveExceedsWindowError"
    assert "skill 'code-reviewer' inference.max_output_tokens (4000)" in failed.data["error"]
    assert client.ledger.requests() == []


async def test_compact_now_target_is_exact_under_declared_cap(
    tmp_path: Path, threads_dir: Path, sim_client: Any,
) -> None:
    """CompactNow(target_tokens) 按生效可用窗口折算比例：soft_limit 恰为 target，预留保留。"""
    client = sim_client(turns=[SimTurn(text="ok")])
    strategy = _RecordingStrategy()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path, entry_max=2_000), threads_dir=threads_dir,
        model_client=client, compressors=[strategy], budget=ContextBudget(context_window=4000),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        await run_until_root_done(engine, taifeng.UserMessage(text="hi"))
        strategy.seen.clear()
        # CompactNow 成功时不发 turn 终态；记录策略不压缩，以「被咨询」作为完成信号
        await engine.submit(CompactNow(target_tokens=500))
        await wait_for_condition(lambda: bool(strategy.seen))
    finally:
        await pool.close()

    [(phase, reserve, soft)] = strategy.seen
    assert (phase, reserve) == ("manual", 2_000)
    assert soft in (499, 500)  # int(可用窗口 × target / 可用窗口)，浮点取整至多差 1
