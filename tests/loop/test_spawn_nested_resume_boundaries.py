"""detached spawn 嵌套 HITL 续跑 —— 边界对抗验证（补 resume_spawn_nested 未覆盖路径）。

现有 test_detached_spawn_nested_hitl.py 只覆盖**深度 2**（spawn 子=专科、leaf=子步），
``resume_spawn_nested`` 的中间父层循环 ``for level in range(len(chain)-2, 0, -1)`` 在
chain==2 时为空循环、从未执行。本文件补三个高风险盲区：

1. **深度 3 嵌套**（spawn→l1→l2→leaf）：逼出中间父层逐层回填循环（level=1 真实执行）。
2. **entry-target spawn**：dispatch.py 新增 ``allow_entry_target`` 放开 spawn 调 entry 门，
   验证 entry 目标可 spawn、且白名单门仍拦截非白名单目标（其余门未被一并放开）。
3. **嵌套 resume 完成后 join-barrier 触发**：MDT 核心——专科嵌套挂起→resume 完成后，
   登记的 join-barrier 应正常 fire（聚合 turn 起跑）。
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
    """轮询等待条件成立（每次 10ms）。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _skill(name: str, *, entry: bool = False, children: list[str] | None = None,
           tools: list[str] | None = None, depth: int = 5) -> str:
    """生成一个 composite SKILL.md（body 含唯一 MARK = 大写下划线 name + _MARK）。"""
    mark = name.upper().replace("-", "_") + "_MARK"
    lines = [
        "---", f"name: {name}", f"description: {name}", "version: 1.0.0",
        "type: composite", "model: mock-model",
    ]
    if entry:
        lines.append("entry: true")
    if children:
        lines.append(f"child_skills: [{', '.join(children)}]")
    if tools:
        lines.append(f"tool_names: [{', '.join(tools)}]")
    lines += [f"max_call_depth: {depth}", "---", f"# {name} {mark}", ""]
    return "\n".join(lines)


def _write_skills(root, spec: dict[str, str]):
    """把 {子目录名: SKILL.md 文本} 写进 skills 目录。"""
    for sub, body in spec.items():
        d = root / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return root


# ───────────────────────── 1. 深度 3 嵌套 ─────────────────────────

@pytest.fixture
def d3_skills(tmp_path):
    """orch3(entry) → d3l1 → d3l2 → d3l3(leaf, HITL)。"""
    return _write_skills(tmp_path / "d3", {
        "orch3": _skill("orch3", entry=True, children=["d3l1"]),
        "d3l1": _skill("d3l1", children=["d3l2"]),
        "d3l2": _skill("d3l2", children=["d3l3"]),
        "d3l3": _skill("d3l3", tools=["request_user_input"]),
    })


@pytest.mark.asyncio
async def test_spawn_nested_depth3_resume(d3_skills, threads_dir):
    """深度 3：spawn d3l1 → call d3l2 → call d3l3 → leaf HITL 挂起 → Resume → 逐层回填
    （中间父层 d3l2 经 _resume_parent_level 续跑）→ 根 d3l1 重跑 → spawn_completed。"""
    client = RoutingMockClient(routes={
        "D3L1_MARK": [
            MockTurn(text="L1 调用 L2。", tool_calls=[
                {"id": "c_l2", "name": "call_skill",
                 "arguments": '{"skill_id": "d3l2", "args": {}}'}]),
            MockTurn(text="L1 最终 D3_DONE"),
        ],
        "D3L2_MARK": [
            MockTurn(text="L2 调用 L3。", tool_calls=[
                {"id": "c_l3", "name": "call_skill",
                 "arguments": '{"skill_id": "d3l3", "args": {}}'}]),
            MockTurn(text="L2 结论 L2_OK"),
        ],
        "D3L3_MARK": [
            MockTurn(text="L3 补问。", tool_calls=[
                {"id": "l3_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "L3 补充"}'}]),
            MockTurn(text="L3 结论 L3_OK"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=d3_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="d3", entry_skill_id="orch3")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="d3l1", args={}, reason="深度3")
    hid, child_tid = sp["handle_id"], sp["child_thread_id"]

    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), \
        "深度3 链未挂起"

    def _leaf_req():
        for ev in events:
            if ev.msg.kind != "turn_suspended":
                continue
            for p in ev.msg.data.get("pending") or []:
                if p.get("reason") == "data":
                    return p["request_id"]
        return None

    assert await _wait(lambda: _leaf_req() is not None), "未观测 leaf DATA 挂起"
    await engine.submit(Resume(
        thread_id=child_tid, resolutions={_leaf_req(): {"answer": "L3 补料"}}))

    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), \
        "深度3 嵌套续跑未完成（中间父层回填链缺口）"
    assert engine.spawn_status([hid])[hid]["result"] == "L1 最终 D3_DONE"

    # 中间层与 leaf 的结论都真实进了各自子 thread 历史（逐层回填成立）
    items = [it async for it in await pool.store.load_thread(child_tid)]
    blob = " ".join(getattr(it, "text", "") or str(it.payload) for it in items)
    assert "L2_OK" in blob, "中间父层 d3l2 续跑结论未回填到根 thread"

    # 幂等：完成后再次 Resume 同一 thread（如用户双击）→ 不崩溃、句柄保持 done。
    await engine.submit(Resume(
        thread_id=child_tid, resolutions={"any": {"answer": "重复"}}))
    await asyncio.sleep(0.2)
    assert engine.spawn_status([hid])[hid]["status"] == "done", \
        "完成后重复 Resume 破坏了终态句柄（幂等违例）"
    assert engine.spawn_status([hid])[hid]["result"] == "L1 最终 D3_DONE"

    watch_task.cancel()
    await pool.close()


# ───────────────────── 2. entry-target spawn ─────────────────────

@pytest.fixture
def entry_target_skills(tmp_path):
    """orch-et(entry) → 白名单含 et-target(entry=true) + unlisted 不在白名单。"""
    return _write_skills(tmp_path / "et", {
        "orch-et": _skill("orch-et", entry=True, children=["et-target"]),
        "et-target": _skill("et-target", entry=True, tools=["request_user_input"]),
        "unlisted": _skill("unlisted", tools=["request_user_input"]),
    })


@pytest.mark.asyncio
async def test_spawn_entry_target_allowed(entry_target_skills, threads_dir):
    """allow_entry_target：spawn 一个 entry=true 目标应放行（修复前 cannot_call_entry）。"""
    client = RoutingMockClient(routes={
        "ET_TARGET_MARK": [MockTurn(text="entry 目标完成 ET_DONE")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=entry_target_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="et", entry_skill_id="orch-et")

    sp = await engine.spawn_skill(skill_id="et-target", args={}, reason="entry目标")
    hid = sp["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), \
        "entry 目标 spawn 未完成（allow_entry_target 未生效）"
    assert engine.spawn_status([hid])[hid]["result"] == "entry 目标完成 ET_DONE"
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_non_whitelisted_still_rejected(entry_target_skills, threads_dir):
    """白名单门未被一并放开：spawn 非白名单目标仍拒（allow_entry_target 只豁免 entry 门）。"""
    client = RoutingMockClient(routes={})
    pool = await taifeng.EnginePool.create(
        skills_dir=entry_target_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="et2", entry_skill_id="orch-et")

    with pytest.raises(ValueError, match="dispatch_rejected"):
        await engine.spawn_skill(skill_id="unlisted", args={}, reason="非白名单")
    await pool.close()


# ───────────── 3. 嵌套 resume 完成后 join-barrier 触发 ─────────────

@pytest.fixture
def barrier_skills(tmp_path):
    """orch-b(entry) → b-expert(嵌套 composite) → b-step(leaf HITL) + consult(聚合)。"""
    return _write_skills(tmp_path / "b", {
        "orch-b": _skill("orch-b", entry=True, children=["b-expert", "consult"]),
        "b-expert": _skill("b-expert", children=["b-step"]),
        "b-step": _skill("b-step", tools=["request_user_input"]),
        "consult": _skill("consult", tools=["request_user_input"]),
    })


@pytest.mark.asyncio
async def test_join_barrier_fires_after_nested_resume(barrier_skills, threads_dir):
    """专科嵌套挂起 → 登记 join-barrier → Resume 完成专科 → barrier 应 fire（聚合起跑）。"""
    client = RoutingMockClient(routes={
        "B_EXPERT_MARK": [
            MockTurn(text="专科调子步。", tool_calls=[
                {"id": "c_step", "name": "call_skill",
                 "arguments": '{"skill_id": "b-step", "args": {}}'}]),
            MockTurn(text="专科完成 B_EXPERT_DONE"),
        ],
        "B_STEP_MARK": [
            MockTurn(text="子步补问。", tool_calls=[
                {"id": "s_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "补充"}'}]),
            MockTurn(text="子步结论 B_STEP_OK"),
        ],
        "CONSULT_MARK": [MockTurn(text="会诊聚合 CONSULT_DONE")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=barrier_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="b", entry_skill_id="orch-b")

    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="b-expert", args={}, reason="专科")
    hid, child_tid = sp["handle_id"], sp["child_thread_id"]
    # 登记 join-barrier：专科终态后起 consult 聚合
    await engine.set_join_barrier([hid], then_skill_id="consult")

    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), "专科未挂起"

    def _leaf_req():
        for ev in events:
            if ev.msg.kind != "turn_suspended":
                continue
            for p in ev.msg.data.get("pending") or []:
                if p.get("reason") == "data":
                    return p["request_id"]
        return None

    assert await _wait(lambda: _leaf_req() is not None), "未观测 leaf DATA 挂起"
    await engine.submit(Resume(
        thread_id=child_tid, resolutions={_leaf_req(): {"answer": "补料"}}))

    # 嵌套续跑完成 → join-barrier 应 fire
    assert await _wait(
        lambda: any(ev.msg.kind == "join_barrier_fired" for ev in events)), \
        "嵌套 resume 完成后 join-barrier 未触发"
    fired = next(ev for ev in events if ev.msg.kind == "join_barrier_fired")
    then_tid = fired.msg.data["then_thread_id"]

    # 聚合 turn 真实跑完（其 thread 完成 + 结论落历史；assistant_text 是分片流式，
    # 故按 then_thread_id 完成 + 持久化历史判定，不靠单条 delta 含完整串）。
    assert await _wait(
        lambda: any(
            ev.msg.kind == "turn_completed" and ev.submission_id == then_tid
            for ev in events)), "consult 聚合 turn 未完成"
    items = [it async for it in await pool.store.load_thread(then_tid)]
    blob = " ".join(getattr(it, "text", "") or str(it.payload) for it in items)
    assert "CONSULT_DONE" in blob, "consult 聚合结论未落 then thread 历史"

    watch_task.cancel()
    await pool.close()
