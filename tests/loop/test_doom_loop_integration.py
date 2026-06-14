"""doom-loop 端到端 —— 反复同 (tool,args) 成功调用 → 先警(注中性事实)后断。

验证 DoomLoopDetector 接进主循环：N=2 警(emit doom_loop_warned + 注
system_injection(source="doom_loop"))、2N=4 断(emit doom_loop_circuit_open +
迭代边界以 end_reason="doom_loop_circuit_open" 终止)。
"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.tool.spec import ToolResult, ToolSpec


def _echo_tool() -> ToolSpec:
    """极简 echo 工具（parallel_safe，每次成功返回 ok）。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True)


def _entry_skill(root):
    """composite entry，tool_names:[echo]。"""
    d = root / "entry"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: entry\ndescription: entry\nversion: 1.0.0\n"
        "type: composite\nentry: true\nmodel: mock-model\n"
        "tool_names: [echo]\nmax_call_depth: 2\n"
        "---\n# entry ENTRY_MARK\n",
        encoding="utf-8")
    return root


def _collect(engine):
    events: list = []

    async def _run():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    return events, asyncio.create_task(_run())


async def _wait(cond, tries: int = 400) -> bool:
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_doom_loop_warn_then_open(tmp_path, threads_dir):
    """同一 echo 调用反复出现：第 2 次警（注提示）、第 4 次断（end_reason 终止）。"""
    skills = _entry_skill(tmp_path / "dl")
    # 6 轮都发同一个 (echo, {}) 调用（call_id 唯一、name+args 恒同 → 同签名）
    same_turns = [
        SimTurn(text="再来一次", tool_calls=[
            {"id": f"c{i}", "name": "echo", "arguments": "{}"}])
        for i in range(6)]
    client = RoutingSimClient(routes={"ENTRY_MARK": same_turns})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool()], max_iterations=10,
        doom_loop_config=taifeng.DoomLoopConfig(max_consecutive_repeats=2))
    engine = await pool.get_or_create(session_id="dl", entry_skill_id="entry")
    events, task = _collect(engine)
    await asyncio.sleep(0)

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))

    # 先警：doom_loop_warned（第 2 次同签名）
    assert await _wait(
        lambda: any(ev.msg.kind == "doom_loop_warned" for ev in events)), "未先警"
    warned = next(ev for ev in events if ev.msg.kind == "doom_loop_warned")
    assert warned.msg.data["tool"] == "echo"
    assert warned.msg.data["consecutive"] == 2
    assert warned.msg.data["threshold"] == 2

    # 后断：doom_loop_circuit_open（第 4 次 = 2N）
    assert await _wait(
        lambda: any(ev.msg.kind == "doom_loop_circuit_open" for ev in events)), "未断路"
    opened = next(ev for ev in events if ev.msg.kind == "doom_loop_circuit_open")
    assert opened.msg.data["consecutive"] == 4

    # turn 以 doom_loop_circuit_open 终止
    assert await _wait(
        lambda: any(ev.msg.kind in ("turn_completed", "turn_failed")
                    and ev.submission_id == sub_id
                    and ev.msg.data.get("end_reason") == "doom_loop_circuit_open"
                    for ev in events)), "turn 未以 doom_loop_circuit_open 终止"

    # history 落一条 source=doom_loop 的中性事实注入（且不含产品祈使词）
    items = engine.history_snapshot()
    notes = [it for it in items
             if it.kind == "system_injection"
             and it.payload.get("source") == "doom_loop"]
    assert len(notes) == 1, f"应恰一条 doom_loop 注入，实得 {len(notes)}"
    text = notes[0].payload["text"].lower()
    assert "echo" in text and "identical" in text
    for word in ("should", "must", "please", "try", "instead"):
        assert word not in text, f"中性事实不应含祈使词：{word!r}"

    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_no_doom_loop_when_disabled(tmp_path, threads_dir):
    """不配 doom_loop_config（默认 None=off）→ 反复同调用也零检测（行为零变化）。"""
    skills = _entry_skill(tmp_path / "dl2")
    # 3 轮同调用（call_id 唯一）后给一个无 tool 的收尾，turn 自然完成
    turns = [
        SimTurn(text="再来", tool_calls=[
            {"id": f"c{i}", "name": "echo", "arguments": "{}"}])
        for i in range(3)]
    turns.append(SimTurn(text="DONE"))
    client = RoutingSimClient(routes={"ENTRY_MARK": turns})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool()], max_iterations=10)
    engine = await pool.get_or_create(session_id="dl2", entry_skill_id="entry")
    events, task = _collect(engine)
    await asyncio.sleep(0)

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    assert await _wait(
        lambda: any(ev.msg.kind == "turn_completed"
                    and ev.submission_id == sub_id for ev in events)), "turn 未完成"
    assert not [ev for ev in events
                if ev.msg.kind in ("doom_loop_warned", "doom_loop_circuit_open")], \
        "未启用时不应有任何 doom-loop 事件"

    task.cancel()
    await pool.close()
