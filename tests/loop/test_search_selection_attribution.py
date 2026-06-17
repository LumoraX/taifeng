"""search_skills 召回溯源 → SkillExecutionRecord selection 字段填充 E2E 测试。

覆盖「相位 2 召回链连回 v1 战绩沉淀」的一刀（T7）：当一次 ``call_skill`` 派发的
目标 skill **是经 ``search_skills`` 召回选中的**，``_spawn_sub_runner`` 落的
``SkillExecutionRecord`` 应填成 ``selection_origin="discovered"`` +
``selection_confidence=该候选的 confidence``；否则保持 v1 行为
（``"whitelist"`` / ``None``）。

三个场景：
    1. 同 turn search→call：先 search_skills 召回目标 + confidence，再 call_skill
       派发 → 断言 discovered + 该 confidence。
    2. 无 search 的 call（向后兼容）：直接 call_skill 不经 search → whitelist / None。
    3. C5 跨 turn 退化：search 在前一 turn、call 在后一 turn → 退化 whitelist / None
       （溯源映射是 TurnRunner turn 内态、不持久化，明确语义非 bug）。

harness 复用 tests/loop/test_skill_outcome_wiring.py 的 EnginePool + SimClient +
_run_until_outer_done + _collect_outcome_items；entry 用 ``child_recall: deferred``
强制暴露 search_skills 工具。召回后端注入确定性 stub（固定 confidence），使
discovered 场景的 confidence 断言精确、与默认 KeywordSkillRecall 内部打分解耦。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.recall import RecallEntry


# ====================================================================
# fixtures —— entry(composite, deferred 召回) → leaf(atomic)
# ====================================================================

# child_recall: deferred 强制暴露 search_skills 工具（无论 child 数多少）。
_ENTRY_BODY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [leaf]
tool_names: []
max_call_depth: 6
exposure:
  child_recall: deferred
---
# entry body
"""

_LEAF_BODY = """---
name: leaf
description: 叶子层，处理具体子任务
version: 1.0.0
type: atomic
---
# leaf body
"""


def _build_skills(tmp_path: Path) -> Path:
    """构造 entry / leaf 两层 skill 目录，返回 skills 目录路径。"""
    skills = tmp_path / "skills"

    entry_dir = skills / "entry"
    entry_dir.mkdir(parents=True)
    (entry_dir / "SKILL.md").write_text(_ENTRY_BODY, encoding="utf-8")

    leaf_dir = skills / "leaf"
    leaf_dir.mkdir(parents=True)
    (leaf_dir / "SKILL.md").write_text(_LEAF_BODY, encoding="utf-8")

    return skills


# 固定召回 confidence：用确定性 stub 钉死，使 discovered 场景断言精确、
# 与默认 KeywordSkillRecall 的 BM25 打分内部实现解耦。
_FIXED_CONFIDENCE = 0.73


class _FixedRecall:
    """测试用确定性召回后端：对池内每个候选返回固定 confidence（实现 SkillRecall 协议）。

    不依赖 query 内容——只要被调到就把 pool 内候选全数返回（截 top_k），confidence
    恒为 ``_FIXED_CONFIDENCE``。这样溯源映射登记的 confidence 是已知常量，断言精确。
    """

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list:
        """返回 pool 内前 top_k 个候选，confidence 恒为 _FIXED_CONFIDENCE。"""
        from taifeng.skill.recall import SkillCandidate

        return [
            SkillCandidate(
                skill_id=e.skill_id,
                description=e.description,
                score=1.0,
                confidence=_FIXED_CONFIDENCE,
                matched_snippet=None,
            )
            for e in pool[:top_k]
        ]


async def _run_until_outer_done(
    engine: taifeng.AgentEngine,
    message: taifeng.UserMessage,
    *,
    deadline_seconds: float = 10.0,
) -> tuple[str, list]:
    """提交 message；subscribe_all 收集事件直到「outermost turn 完成」。

    与 test_skill_outcome_wiring.py 同构：通过 skill_dispatched / skill_returned
    计数推断嵌套深度，仅在 depth==0 且终结时退出。返回 (sub_id, events)。
    """
    sub_id_holder: list[str] = []
    events: list = []
    done = asyncio.Event()

    async def collector() -> None:
        depth = 0
        async for ev in engine.subscribe_all():
            if not sub_id_holder or ev.submission_id != sub_id_holder[0]:
                continue
            events.append(ev)
            if ev.msg.kind == "skill_dispatched":
                depth += 1
            elif ev.msg.kind == "skill_returned":
                depth -= 1
            if ev.msg.kind in ("turn_completed", "turn_failed") and depth == 0:
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)  # 让 collector 先注册 subscribe_all 队列
    sub_id = await engine.submit(message)
    sub_id_holder.append(sub_id)
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return sub_id, events


def _outcome_records(events: list) -> list:
    """从事件流摘出 skill_outcome_recorded 的 data 负载列表。"""
    return [
        ev.msg.data for ev in events if ev.msg.kind == "skill_outcome_recorded"
    ]


# ====================================================================
# 1. 同 turn search→call → discovered + 召回 confidence
# ====================================================================

@pytest.mark.asyncio
async def test_search_then_call_records_discovered(tmp_path: Path) -> None:
    """同一 turn 内先 search_skills 召回 leaf，再 call_skill 派发 leaf。

    断言落的 SkillExecutionRecord：
        selection_origin == "discovered"
        selection_confidence == _FIXED_CONFIDENCE（召回 stub 固定值）

    Mock turn 序列：
        1. entry turn 1: search_skills(query)  —— 召回登记 leaf→discovered/0.73
        2. entry turn 1（续）: call_skill(leaf) —— 派发（同 turn，溯源命中）
        3. leaf turn 1: 终结文本（无 tool call）
        4. entry turn 2: 终结文本给用户
    """
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads"
    threads.mkdir()

    u = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    client = SimClient(turns=[
        # entry 首次采样：调 search_skills（召回 leaf）
        SimTurn(
            text="先搜索可用子 skill",
            tool_calls=[{
                "id": "tc_search",
                "name": "search_skills",
                "arguments": '{"query": "处理子任务"}',
            }],
            usage=u,
        ),
        # entry 第二次采样（同 turn 续）：据召回结果 call_skill 派发 leaf
        SimTurn(
            text="派发到召回到的 leaf",
            tool_calls=[{
                "id": "tc_leaf",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {"q": "y"}}',
            }],
            usage=u,
        ),
        # leaf 终结
        SimTurn(text="leaf 输出：完成", usage=u),
        # entry 总结
        SimTurn(text="entry 总结：子任务完成", usage=u),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
        skill_recall=_FixedRecall(),
    )
    engine = await pool.get_or_create(
        session_id="s-discovered", entry_skill_id="entry",
    )

    _sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="请搜索并派发"),
    )

    records = _outcome_records(events)
    assert len(records) == 1, [ev.msg.kind for ev in events]
    data = records[0]
    assert data["skill_id"] == "leaf", data
    assert data["selection_origin"] == "discovered", (
        f"经 search_skills 召回派发应记 discovered，实际 {data['selection_origin']!r}"
    )
    assert data["selection_confidence"] == _FIXED_CONFIDENCE, (
        f"selection_confidence 应为召回候选 confidence={_FIXED_CONFIDENCE}，"
        f"实际 {data['selection_confidence']!r}"
    )
    await pool.close()


# ====================================================================
# 2. 无 search 的 call（向后兼容）→ whitelist / None
# ====================================================================

@pytest.mark.asyncio
async def test_call_without_search_records_whitelist(tmp_path: Path) -> None:
    """直接 call_skill 不经 search_skills → 保持 v1 行为 whitelist / None。"""
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads_nosearch"
    threads.mkdir()

    u = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    client = SimClient(turns=[
        SimTurn(
            text="直接派发 leaf（不搜索）",
            tool_calls=[{
                "id": "tc_leaf",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {"q": "y"}}',
            }],
            usage=u,
        ),
        SimTurn(text="leaf 输出：完成", usage=u),
        SimTurn(text="entry 总结：子任务完成", usage=u),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
        skill_recall=_FixedRecall(),
    )
    engine = await pool.get_or_create(
        session_id="s-nosearch", entry_skill_id="entry",
    )

    _sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="直接派发"),
    )

    records = _outcome_records(events)
    assert len(records) == 1, [ev.msg.kind for ev in events]
    data = records[0]
    assert data["skill_id"] == "leaf", data
    assert data["selection_origin"] == "whitelist", (
        f"未经 search 的派发应记 whitelist，实际 {data['selection_origin']!r}"
    )
    assert data["selection_confidence"] is None, (
        f"未经 search 的派发 selection_confidence 应为 None，"
        f"实际 {data['selection_confidence']!r}"
    )
    await pool.close()


# ====================================================================
# 3. C5 跨 turn 退化：search 在前一 turn、call 在后一 turn → 退化 whitelist / None
# ====================================================================

@pytest.mark.asyncio
async def test_cross_turn_search_then_call_degrades(tmp_path: Path) -> None:
    """search 在 turn A、call 在 turn B（新 TurnRunner）→ 溯源映射退化 whitelist / None。

    溯源映射是 TurnRunner turn 内态、不持久化（C5）；后一 turn 是新 TurnRunner，
    前一 turn 的召回登记不可见，故该 call 退化为 v1 行为——这是**明确语义、非 bug**。

    构造：
        turn 1（用户消息 1）: entry 仅调 search_skills（召回 leaf），不 call → turn 完成。
        turn 2（用户消息 2）: entry call_skill(leaf) 派发 → leaf 终结 → entry 总结。
    断言 turn 2 派发落的记录退化为 whitelist / None。
    """
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads_crossturn"
    threads.mkdir()

    u = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads,
        # turn 1：只 search 后直接收尾（无 call）；turn 2：call_skill 派发 leaf。
        model_client=SimClient(turns=[
            # —— turn 1 ——
            SimTurn(
                text="本轮只搜索",
                tool_calls=[{
                    "id": "tc_search_t1",
                    "name": "search_skills",
                    "arguments": '{"query": "处理子任务"}',
                }],
                usage=u,
            ),
            SimTurn(text="搜索完毕，本轮结束。", usage=u),
            # —— turn 2 ——
            SimTurn(
                text="本轮派发上轮搜到的 leaf",
                tool_calls=[{
                    "id": "tc_leaf_t2",
                    "name": "call_skill",
                    "arguments": '{"skill_id": "leaf", "args": {"q": "y"}}',
                }],
                usage=u,
            ),
            SimTurn(text="leaf 输出：完成", usage=u),
            SimTurn(text="entry 总结：子任务完成", usage=u),
        ]),
        compressors=[],
        skill_recall=_FixedRecall(),
    )
    engine = await pool.get_or_create(
        session_id="s-crossturn", entry_skill_id="entry",
    )

    # turn 1：只 search，不应产生任何 skill_outcome（没派发子 skill）
    _sid1, events1 = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="先搜索"),
    )
    assert _outcome_records(events1) == [], (
        "turn 1 仅 search 不 call，不应落 skill_outcome"
    )

    # turn 2：新 TurnRunner，call_skill 派发 leaf —— 溯源映射已随上一 TurnRunner 销毁
    _sid2, events2 = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="再派发"),
    )
    records = _outcome_records(events2)
    assert len(records) == 1, [ev.msg.kind for ev in events2]
    data = records[0]
    assert data["skill_id"] == "leaf", data
    assert data["selection_origin"] == "whitelist", (
        f"跨 turn 退化应记 whitelist（C5：溯源 turn 内态不持久化），"
        f"实际 {data['selection_origin']!r}"
    )
    assert data["selection_confidence"] is None, (
        f"跨 turn 退化 selection_confidence 应为 None，实际 "
        f"{data['selection_confidence']!r}"
    )
    await pool.close()
