"""run_sub_skill 终态沉淀战绩记录的接线 E2E 测试（认知回路 ⑦ 沉淀）。

覆盖 `src/taifeng/loop/turn.py::_spawn_sub_runner` 在子 skill 终态后：
    - 判战绩（StructuralOutcomeJudge 默认结构性判定）
    - 落旁路 skill_outcome ResponseItem（不进 LLM 消息序列）
    - emit SkillOutcomeRecorded 事件

并断言挂起路径（子 skill 内 HITL 挂起）**不**触发任何沉淀——挂起在
`_spawn_sub_runner` 内提前 raise SuspendSignal 返回，记录块根本不执行。

harness 复用 tests/skill/test_composite_e2e.py 的 EnginePool + SimClient +
subscribe_all + 深度计数器（_run_until_outer_done）；挂起场景复用
tests/test_child_suspend_resume.py 的 permission-gated 子 skill 挂起范式。
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
    from pathlib import Path


# ====================================================================
# fixtures —— 构造 entry(composite) → leaf(atomic) 两层 skill
# ====================================================================

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
---
# entry body
"""

_LEAF_BODY = """---
name: leaf
description: 叶子层
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


async def _run_until_outer_done(
    engine: taifeng.AgentEngine,
    message: taifeng.UserMessage,
    *,
    deadline_seconds: float = 10.0,
) -> tuple[str, list]:
    """提交 message；subscribe_all 收集事件直到「outermost turn 完成」。

    子 skill 的 turn_completed 与 entry 同类型，通过 skill_dispatched /
    skill_returned 计数推断当前嵌套深度，仅在 depth==0 且终结时退出。
    返回 (sub_id, events)，events 已按 sub_id 过滤。
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
            if (
                ev.msg.kind in ("turn_completed", "turn_failed")
                and depth == 0
            ):
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


async def _collect_outcome_items(pool: taifeng.EnginePool) -> list:
    """扫描所有 thread 的持久化 item，收集 kind=='skill_outcome' 的旁路记录。"""
    items: list = []
    for info in await pool.store.list_threads():
        async for it in await pool.store.load_thread(info.thread_id):
            if it.kind == "skill_outcome":
                items.append(it)
    return items


# ====================================================================
# 1. 子 skill 成功终态 → 沉淀战绩记录 + emit 事件
# ====================================================================

@pytest.mark.asyncio
async def test_sub_skill_dispatch_records_outcome(tmp_path: Path) -> None:
    """entry 派发 leaf（成功）→ 恰一条 SkillOutcomeRecorded + 一条落盘 skill_outcome。

    Mock 3 个 turn：
        1. entry turn 1: call_skill(leaf)
        2. leaf turn 1: 终结文本（无 tool call）
        3. entry turn 2: 终结文本给用户
    """
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads"
    threads.mkdir()

    client = SimClient(turns=[
        SimTurn(
            text="派发到 leaf",
            tool_calls=[{
                "id": "tc_leaf",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {"q": "y"}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        SimTurn(
            text="leaf 输出：完成",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        SimTurn(
            text="entry 总结：子任务完成",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s-outcome", entry_skill_id="entry",
    )

    _sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="请派发 leaf"),
    )

    # —— 断言事件：恰一条 SkillOutcomeRecorded，字段符合结构性判定 ——
    recorded = [
        ev for ev in events if ev.msg.kind == "skill_outcome_recorded"
    ]
    assert len(recorded) == 1, [ev.msg.kind for ev in events]
    data = recorded[0].msg.data
    assert data["outcome"] == "success", data
    assert data["outcome_signal_source"] == "structural", data
    assert data["selection_origin"] == "whitelist", data
    assert data["selection_confidence"] is None, data
    assert data["skill_id"] == "leaf", data
    assert data["cost_iterations"] >= 1, data

    # —— 断言落盘：恰一条 skill_outcome item，outcome==success ——
    outcome_items = await _collect_outcome_items(pool)
    await pool.close()
    assert len(outcome_items) == 1, [it.payload for it in outcome_items]
    assert outcome_items[0].payload["outcome"] == "success", (
        outcome_items[0].payload
    )


# ====================================================================
# 2. 子 skill 挂起 → 不沉淀（提前 raise SuspendSignal 返回）
# ====================================================================

# 父 entry skill：call_skill 派发 child，自身不挂工具
_SUSPEND_PARENT = """---
name: parent-orch
description: 父编排
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [child-worker]
tool_names: []
max_call_depth: 3
---
# 父编排 ENTRY_MARK
派发子 skill 完成具体工作。
"""

# 子 skill：声明需审批的 danger 工具，body 唯一标记 CHILD_MARK。
# composite 须有 child_skills，挂一个 leaf 占位（本用例不实际派发它）。
_SUSPEND_CHILD = """---
name: child-worker
description: 子工作单元
version: 1.0.0
type: composite
model: mock-model
child_skills: [leaf-noop]
tool_names: [danger]
max_call_depth: 2
---
# 子工作单元 CHILD_MARK
调用 danger 工具完成工作。
"""

_SUSPEND_LEAF = """---
name: leaf-noop
description: 叶子占位
version: 1.0.0
type: atomic
---
# 叶子占位
不做实际工作。
"""


def _build_suspend_skills(tmp_path: Path) -> Path:
    """内联写出 parent-orch(entry) + child-worker + leaf-noop，返回 skills 目录。"""
    skills = tmp_path / "suspend_skills"
    for sub, body in (
        ("parent-orch", _SUSPEND_PARENT),
        ("child-worker", _SUSPEND_CHILD),
        ("leaf-noop", _SUSPEND_LEAF),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


async def _gated_danger_tool():
    """构造 ask 门控的 danger 工具：门控时透传 ctx.call_id 进 PermissionRequest.metadata。

    与 tests/test_child_suspend_resume.py::_gated_danger_tool 同构 ——
    SuspendingPrompter 据 metadata.call_id 把 related_call_id 填进 PendingRequest，
    使挂起点落在【子 thread】内的 danger 调用上。
    """
    from taifeng.permission.types import PermissionRequest
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    async def handler(args, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        req = PermissionRequest.for_tool_call(
            "danger",
            args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("child",),
            extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        return ToolResult.ok("danger executed in child")

    return ToolSpec(
        name="danger",
        description="需审批的危险工具（子 skill 内，测试用）",
        input_schema={
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,
    )


def _suspend_routing_client():
    """RoutingSimClient：父首轮 call_skill 派子；子首轮 danger（挂起）。"""
    from taifeng.llm.providers import SimTurn
    from taifeng.llm.providers.sim import RoutingSimClient

    return RoutingSimClient(routes={
        "ENTRY_MARK": [
            SimTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments":
                     '{"skill_id": "child-worker", "reason": "do work"}'},
            ]),
            SimTurn(text="父编排完成。"),
        ],
        "CHILD_MARK": [
            SimTurn(text="子调用 danger", tool_calls=[
                {"id": "call_d1", "name": "danger", "arguments": "{}"},
            ]),
            SimTurn(text="子工作完成。"),
        ],
    })


@pytest.mark.asyncio
async def test_suspended_sub_skill_emits_no_record(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """子 skill 内 HITL 挂起 → 无 SkillOutcomeRecorded 事件、无 skill_outcome 落盘。

    挂起在 _spawn_sub_runner 内提前 raise SuspendSignal 返回，记录块根本不执行。
    """
    from taifeng.permission.types import (
        PermissionPolicy,
        PermissionRule,
        SuspendingPrompter,
    )

    skills = _build_suspend_skills(tmp_path)
    gated_tool = await _gated_danger_tool()
    # 放行 skill_dispatch（call_skill 不挂起），仅 default ask 门控子 skill 内的
    # danger（scope=tool_use）→ 挂起点落在【子 thread】而非父的 call_skill 派发 gate。
    policy = PermissionPolicy(
        default_mode="ask",
        rules=[PermissionRule(
            scope="skill_dispatch", target_pattern="glob:*", mode="allow",
        )],
        prompter=SuspendingPrompter(),
    )

    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=threads_dir,
        model_client=_suspend_routing_client(),
        compressors=[],
        extra_tools=[gated_tool],
        permission_policy=policy,
    )
    engine = await pool.get_or_create(
        session_id="s-suspend", entry_skill_id="parent-orch",
    )

    # 后台全量收集器：本场景跨子 turn + 根 turn，统一用 subscribe_all 收集，
    # 等待 turn_suspended（任意层）作为终结判据。
    events: list = []
    done = asyncio.Event()

    async def collector() -> None:
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "turn_suspended":
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="go"))
    try:
        await asyncio.wait_for(done.wait(), timeout=10.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # 以挂起收尾
    assert events[-1].msg.kind == "turn_suspended", [
        ev.msg.kind for ev in events
    ]
    # —— 关键断言：挂起路径不沉淀 ——
    recorded = [
        ev for ev in events if ev.msg.kind == "skill_outcome_recorded"
    ]
    assert recorded == [], "子 skill 挂起不应触发 SkillOutcomeRecorded"

    outcome_items = await _collect_outcome_items(pool)
    await pool.close()
    assert outcome_items == [], "子 skill 挂起不应落盘 skill_outcome item"
