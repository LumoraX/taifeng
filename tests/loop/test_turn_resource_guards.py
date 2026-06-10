"""turn-resource-guards e2e —— 断路器提前终止 / refund 不耗预算 / 子预算独立。

走 EnginePool 全栈（permission deny 经 call_skill 真实回流 → turn 单点记账）。
默认零变化已由全量回归守护（denial_breaker_config=None + refunds_iteration=False）。
"""

from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers import MockTurn
from taifeng.llm.providers.mock import RoutingMockClient
from taifeng.loop.denial_breaker import DenialBreakerConfig
from taifeng.permission import PermissionPolicy
from taifeng.tool.spec import ToolResult, ToolSpec

_ENTRY = """---
name: guard-entry
description: 护栏测试入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [guard-sub]
tool_names: [echo]
max_call_depth: 3
---
# 入口 GUARD_ENTRY_MARK
按指令调用工具或子 skill。
"""

_SUB = """---
name: guard-sub
description: 子技能
version: 1.0.0
type: composite
model: mock-model
tool_names: [echo]
max_call_depth: 2
---
# 子技能 GUARD_SUB_MARK
调用 echo 后给结论。
"""


def _echo_tool(*, refunds: bool = False) -> ToolSpec:
    """极简 echo 工具（parallel_safe；refunds 可选）。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
        parallel_safe=True,
        refunds_iteration=refunds,
    )


@pytest.fixture
def guard_skills(tmp_path):
    skills = tmp_path / "skills"
    for sub, body in (("guard-entry", _ENTRY), ("guard-sub", _SUB)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _call_skill_turn(i: int) -> MockTurn:
    return MockTurn(text=f"第{i}次派发。", tool_calls=[
        {"id": f"c{i}", "name": "call_skill",
         "arguments": '{"skill_id": "guard-sub", "args": {}}'}])


async def test_denial_circuit_opens_and_terminates(guard_skills, threads_dir):
    """连续 2 次 permission deny → 断路恰好一次 + end_reason=denial_circuit_open。"""
    client = RoutingMockClient(routes={
        "GUARD_ENTRY_MARK": [_call_skill_turn(1), _call_skill_turn(2),
                             _call_skill_turn(3)],
    })
    policy = PermissionPolicy.from_dict({"deny": ["Skill(*)"]})
    pool = await taifeng.EnginePool.create(
        skills_dir=guard_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], permission_policy=policy,
        denial_breaker_config=DenialBreakerConfig(max_consecutive_denials=2),
    )
    engine = await pool.get_or_create(session_id="g1", entry_skill_id="guard-entry")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    sub_id = await engine.submit(taifeng.UserMessage(text="开始"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await asyncio.sleep(0.1)

    opened = [m for m in events if m.kind == "denial_circuit_open"]
    assert len(opened) == 1, "denial_circuit_open 应恰好一次"
    assert opened[0].data["consecutive"] == 2
    assert opened[0].data["last_denied_target"] == "call_skill"
    done = [m for m in events if m.kind == "turn_completed"]
    assert done and done[0].data["end_reason"] == "denial_circuit_open"
    # 配对完整：第 3 轮未被采样（第 2 圈边界即终止）
    assert done[0].data["iterations"] == 2
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def test_refund_tool_does_not_consume_budget(guard_skills, threads_dir):
    """refunds_iteration 工具成功轮不耗预算：max_iterations=3 跑 5 轮 echo 仍 completed。"""
    turns = [MockTurn(text=f"第{i}轮。", tool_calls=[
        {"id": f"e{i}", "name": "echo", "arguments": "{}"}]) for i in range(5)]
    turns.append(MockTurn(text="完成 REFUND_DONE"))
    client = RoutingMockClient(routes={"GUARD_ENTRY_MARK": turns})
    pool = await taifeng.EnginePool.create(
        skills_dir=guard_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool(refunds=True)],
        max_iterations=3,
    )
    engine = await pool.get_or_create(session_id="g2", entry_skill_id="guard-entry")
    sub_id = await engine.submit(taifeng.UserMessage(text="开始"))
    events: list = []
    async for ev in engine.subscribe(sub_id):
        events.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    done = [m for m in events if m.kind == "turn_completed"][0]
    assert done.data["end_reason"] == "completed", \
        "refund 未生效：3 步预算应撑过 5 轮 echo"
    completed_calls = [m for m in events if m.kind == "tool_call_completed"]
    assert len(completed_calls) == 5
    # 净消费：5 轮全 refund + 收尾文本轮 1 步
    assert done.data["iterations"] == 1
    await pool.close()


async def test_child_budget_independent_e2e(guard_skills, threads_dir):
    """子 turn 独立预算：父 cap=3 耗 1 步派发后，子仍可独立跑满 3 圈。"""
    sub_turns = [MockTurn(text=f"子第{i}轮。", tool_calls=[
        {"id": f"s{i}", "name": "echo", "arguments": "{}"}]) for i in range(2)]
    sub_turns.append(MockTurn(text="子完成 SUB_DONE"))
    client = RoutingMockClient(routes={
        "GUARD_ENTRY_MARK": [_call_skill_turn(1), MockTurn(text="父完成 PARENT_DONE")],
        "GUARD_SUB_MARK": sub_turns,  # 子需 3 圈（2 echo + 1 文本）
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=guard_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool()], max_iterations=3,
    )
    engine = await pool.get_or_create(session_id="g3", entry_skill_id="guard-entry")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="开始"))

    def _root_done():
        return [m for m in events
                if m.kind == "turn_completed" and m.data.get("is_root")]

    for _ in range(300):  # 轮询等 root turn 终态（composite 流多 turn_completed）
        if _root_done():
            break
        await asyncio.sleep(0.01)

    root = _root_done()[0]
    assert root.data["end_reason"] == "completed"
    assert root.data["iterations"] == 2  # 父净耗 2 步（派发 + 收尾），未被子挤兑
    # 子真实跑满 3 圈（若共享父预算只剩 2 步，子会 max_iterations 截断）
    subs = [m for m in events
            if m.kind == "turn_completed" and not m.data.get("is_root")]
    assert subs and subs[0].data["iterations"] == 3
    hist = engine.history_snapshot()
    blob = " ".join(str(it.payload) for it in hist)
    assert "SUB_DONE" in blob, "子 skill 未完整跑完（子预算被父挤兑？）"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def test_prompter_timeout_deny_counts_toward_breaker(guard_skills, threads_dir):
    """HITL ask 超时产生的 deny 同样计入断路器（它就是 deny 结果）。"""
    from taifeng.permission import CallbackPrompter

    async def _never_answers(request):  # 模拟前端永不响应
        await asyncio.sleep(60)

    client = RoutingMockClient(routes={
        "GUARD_ENTRY_MARK": [_call_skill_turn(1), _call_skill_turn(2),
                             _call_skill_turn(3)],
    })
    policy = PermissionPolicy.from_dict(
        {"ask": ["Skill(*)"]},
        prompter=CallbackPrompter(_never_answers),
        prompter_timeout_seconds=0.05,  # 极小超时 → 自动 deny
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=guard_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], permission_policy=policy,
        denial_breaker_config=DenialBreakerConfig(max_consecutive_denials=2),
    )
    engine = await pool.get_or_create(session_id="g4", entry_skill_id="guard-entry")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    sub_id = await engine.submit(taifeng.UserMessage(text="开始"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await asyncio.sleep(0.1)

    opened = [m for m in events if m.kind == "denial_circuit_open"]
    assert len(opened) == 1, "timeout deny 未计入断路器"
    done = [m for m in events if m.kind == "turn_completed"]
    assert done and done[0].data["end_reason"] == "denial_circuit_open"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
