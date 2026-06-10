"""detached spawn + 嵌套（composite 专家的子 skill）错峰 HITL resume 测试。

复现并守护一个真实场景缺口：被 spawn 的子任务是 **composite**（如领域专家 agent，
top turn 通过 call_skill 编排多个子 skill），其**子 skill** 在执行中 request_user_input
挂起 → 专家 top turn 随之以 ``CHILD_SKILL`` 挂起（嵌套），spawn 句柄标 suspended。

修复前缺口：``_resume_spawn`` 只在【spawn 直接子 thread】上找活跃挂起并交
``SuspensionResolver`` 配对——而嵌套场景里该层挂起 reason==CHILD_SKILL（内核内部态、
非用户可直接 resolve），resolver 抛 ``unhandled_suspend_reason: child_skill`` → Rejected，
句柄永久卡 suspended（错峰 HITL 死锁）。真实用户可答的 DATA 挂起埋在更深的 leaf 子 thread。

修复后：``_resume_spawn`` 检测到直接子 thread 是 CHILD_SKILL 挂起 → 走嵌套续跑链
（自 spawn 子 thread 向下串到 leaf，用用户 resolutions 核销 leaf 的 DATA 挂起，逐层
回填父 call_skill output，最终重跑 spawn 子 thread 至终态）→ spawn_completed。
"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers import MockTurn
from taifeng.llm.providers.mock import RoutingMockClient
from taifeng.loop.submission import Resume
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待条件成立（每次 10ms），用于等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


# 专家 agent：composite + entry=false，top turn 通过 call_skill 编排一个子 skill。
_NESTED_EXPERT = """---
name: nested-expert
description: 嵌套专家（top 编排子 skill）
version: 1.0.0
type: composite
model: mock-model
child_skills: [nested-step]
max_call_depth: 3
---
# 嵌套专家 NESTED_EXPERT_MARK
先 call_skill 调用 nested-step，拿到其结论后给最终结论。
"""

# 子 skill（leaf）：composite + tool-only（仅 request_user_input），执行中向用户补料。
_NESTED_STEP = """---
name: nested-step
description: 子步骤（请求用户补料）
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 子步骤 NESTED_STEP_MARK
先 request_user_input 补问，再据答复给结论。
"""

# 编排器（entry）：把嵌套专家列进白名单（spawn 要求 target 在 caller 白名单内）。
_ORCH = """---
name: nested-orch
description: 编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [nested-expert]
max_call_depth: 4
---
# 编排器 NESTED_ORCH_MARK
spawn 嵌套专家。
"""


@pytest.fixture
def nested_skills(tmp_path):
    """nested-orch（entry）+ nested-expert（composite）+ nested-step（leaf）skills 目录。"""
    skills = tmp_path / "nested_skills"
    for sub, body in (
        ("nested-orch", _ORCH),
        ("nested-expert", _NESTED_EXPERT),
        ("nested-step", _NESTED_STEP),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


@pytest.mark.asyncio
async def test_spawn_nested_child_skill_hitl_resume(nested_skills, threads_dir):
    """spawn composite 专家 → 其子 skill HITL 挂起（嵌套 CHILD_SKILL）→ Resume(spawn 子
    thread, leaf 的 request_id) → 子 skill 续跑 → 专家 top 续跑 → spawn_completed。"""
    client = RoutingMockClient(routes={
        # 专家 top：turn1 call_skill(nested-step)；子 skill 回流后 turn2 出最终结论。
        "NESTED_EXPERT_MARK": [
            MockTurn(text="专家编排：调用子步骤。", tool_calls=[
                {"id": "call_step", "name": "call_skill",
                 "arguments": '{"skill_id": "nested-step", "args": {}}'},
            ]),
            MockTurn(text="专家最终结论 EXPERT_DONE"),
        ],
        # 子 skill（leaf）：turn1 request_user_input（挂起）；Resume 后 turn2 出结论。
        "NESTED_STEP_MARK": [
            MockTurn(text="子步骤补问。", tool_calls=[
                {"id": "step_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "子步骤需要补充信息"}'},
            ]),
            MockTurn(text="子步骤结论 STEP_DONE"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=nested_skills,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(
        session_id="nested-hitl", entry_skill_id="nested-orch")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    # 1. spawn 嵌套专家（直接发起，绕开 orchestrator LLM turn，聚焦续跑链）
    sp = await engine.spawn_skill(
        skill_id="nested-expert", args={}, reason="嵌套专家")
    hid, child_tid = sp["handle_id"], sp["child_thread_id"]

    # 2. 等专家句柄挂起（其 top turn 因子 skill CHILD_SKILL 而挂起）
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), \
        "嵌套专家未挂起"

    # 3. 真实用户可答的 DATA 挂起来自 leaf 子 skill turn —— 取其 turn_suspended 事件
    #    （reason=data，携带真实 request_id；submission_id == spawn 子 thread）。
    def _leaf_data_suspend():
        for ev in events:
            if ev.msg.kind != "turn_suspended":
                continue
            for p in ev.msg.data.get("pending") or []:
                if p.get("reason") == "data":
                    return ev, p
        return None

    assert await _wait(lambda: _leaf_data_suspend() is not None), \
        "未观测到 leaf 子 skill 的 DATA 挂起"
    leaf_ev, leaf_pending = _leaf_data_suspend()
    # leaf 子 turn 的 submission_id 与 spawn 子 thread 同源（子 skill 继承父 submission）
    assert leaf_ev.submission_id == child_tid
    req_id = leaf_pending["request_id"]

    # 4. Resume(spawn 子 thread, leaf 的 request_id) —— 业务侧据 SpawnSuspended.thread_id
    #    路由 + leaf turn_suspended 的 request_id 回填。命中挂起 spawn 句柄 → 嵌套续跑链。
    await engine.submit(Resume(
        thread_id=child_tid, resolutions={req_id: {"answer": "已补充：血糖 6.3"}}))

    # 5. 续跑链应让专家跑到终态完成（子 skill 续跑 → 专家 top 续跑 → spawn_completed）
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), \
        "嵌套专家续跑后未完成（嵌套 CHILD_SKILL 续跑链缺口）"
    assert engine.spawn_status([hid])[hid]["result"] == "专家最终结论 EXPERT_DONE"

    watch_task.cancel()
    await pool.close()


# 多轮嵌套错峰：composite 专家编排 2 个子 skill，**两个子 skill 各自挂起一次**（镜像真实
# 医学专家多步流水线里多步先后 request_user_input）—— 验证嵌套续跑链支持多轮、且每轮都能
# 重新挂起 / 重新 resume，专家最终跑完所有步骤。
_EXPERT2 = """---
name: expert2
description: 两步嵌套专家
version: 1.0.0
type: composite
model: mock-model
child_skills: [step-a, step-b]
max_call_depth: 3
---
# 两步专家 EXPERT2_MARK
依次 call_skill 调用 step-a、step-b，各拿结论后给最终结论。
"""

_STEP_A = """---
name: step-a
description: 子步骤 A（补料）
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 子步骤A STEP_A_MARK
先 request_user_input 补问，再给结论。
"""

_STEP_B = """---
name: step-b
description: 子步骤 B（补料）
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 子步骤B STEP_B_MARK
先 request_user_input 补问，再给结论。
"""

_ORCH2 = """---
name: orch2
description: 编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [expert2]
max_call_depth: 4
---
# 编排器 ORCH2_MARK
spawn 两步嵌套专家。
"""


@pytest.fixture
def nested2_skills(tmp_path):
    """orch2（entry）+ expert2（两步 composite）+ step-a / step-b（各自 HITL leaf）。"""
    skills = tmp_path / "nested2_skills"
    for sub, body in (
        ("orch2", _ORCH2),
        ("expert2", _EXPERT2),
        ("step-a", _STEP_A),
        ("step-b", _STEP_B),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


@pytest.mark.asyncio
async def test_spawn_nested_multi_round_hitl_resume(nested2_skills, threads_dir):
    """spawn 两步专家 → step-a 挂起 → Resume → step-b 又挂起（多轮）→ Resume → 全跑完。"""
    client = RoutingMockClient(routes={
        "EXPERT2_MARK": [
            MockTurn(text="调用 step-a。", tool_calls=[
                {"id": "call_a", "name": "call_skill",
                 "arguments": '{"skill_id": "step-a", "args": {}}'}]),
            MockTurn(text="调用 step-b。", tool_calls=[
                {"id": "call_b", "name": "call_skill",
                 "arguments": '{"skill_id": "step-b", "args": {}}'}]),
            MockTurn(text="两步完成 EXPERT2_DONE"),
        ],
        "STEP_A_MARK": [
            MockTurn(text="A 补问。", tool_calls=[
                {"id": "a_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "A 需要补充"}'}]),
            MockTurn(text="A 结论 A_OK"),
        ],
        "STEP_B_MARK": [
            MockTurn(text="B 补问。", tool_calls=[
                {"id": "b_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "B 需要补充"}'}]),
            MockTurn(text="B 结论 B_OK"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=nested2_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="nested2", entry_skill_id="orch2")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="expert2", args={}, reason="两步专家")
    hid, child_tid = sp["handle_id"], sp["child_thread_id"]

    def _suspend_count() -> int:
        """已观测的 spawn_suspended 次数（多轮挂起递增）。"""
        return sum(
            1 for ev in events
            if ev.msg.kind == "spawn_suspended"
            and ev.msg.data.get("handle_id") == hid)

    def _latest_data_req() -> str | None:
        """最近一条 leaf DATA turn_suspended 的 request_id（按观测顺序取末条）。"""
        found = None
        for ev in events:
            if ev.msg.kind != "turn_suspended":
                continue
            for p in ev.msg.data.get("pending") or []:
                if p.get("reason") == "data":
                    found = p["request_id"]
        return found

    # 轮 1：step-a 挂起 → Resume
    assert await _wait(lambda: _suspend_count() >= 1), "step-a 未挂起"
    req1 = _latest_data_req()
    assert req1 is not None
    await engine.submit(
        Resume(thread_id=child_tid, resolutions={req1: {"answer": "a"}}))

    # 轮 2：step-b 再次挂起（多轮）—— 等到第 2 条 spawn_suspended
    assert await _wait(lambda: _suspend_count() >= 2), "step-b 未触发第 2 轮挂起"
    req2 = _latest_data_req()
    assert req2 is not None and req2 != req1
    await engine.submit(
        Resume(thread_id=child_tid, resolutions={req2: {"answer": "b"}}))

    # 全跑完
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), \
        "两步专家多轮续跑后未完成"
    assert engine.spawn_status([hid])[hid]["result"] == "两步完成 EXPERT2_DONE"

    # 链路真实跑过两个子步（A_OK / B_OK 都进了 child thread 历史）
    items = [it async for it in await pool.store.load_thread(child_tid)]
    blob = " ".join(
        getattr(it, "text", "") or str(it.payload) for it in items)
    assert "A_OK" in blob and "B_OK" in blob

    watch_task.cancel()
    await pool.close()
