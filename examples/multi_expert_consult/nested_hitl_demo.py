"""嵌套专科错峰 HITL demo —— 真实 MDT 拓扑（composite 专科 + 子 skill HITL，SimClient）。

与同目录 `demo.py` 的差异：`demo.py` 的专科是 **tool-only composite**（自己直接
`request_user_input`），挂起落在 spawn 子 thread 自身（直接 DATA 挂起）。本 demo 演示
**真实医学 MDT 拓扑**——被 spawn 的专科是 **composite 且通过 `call_skill` 编排子 skill**，
由其**子 skill** 在执行中 `request_user_input` 挂起 → spawn 子 thread 以 `CHILD_SKILL`
（内核内部态）挂起（**嵌套挂起**）。

业务凭 `Resume(thread_id=<spawn 子 thread>, resolutions=...)` 续跑：内核走
`resume_spawn_nested` 续跑链——下探 leaf 核销用户挂起 → 逐层回填父 call_skill output →
重跑 spawn 子 thread 至终态 → `spawn_completed`。

> 缺嵌套续跑链时，本路径会因 `unhandled_suspend_reason: child_skill` 永久卡 suspended
> （嵌套错峰 HITL 死锁）。契约见 docs/architecture/capabilities/detached-spawn.md
> 「嵌套挂起（CHILD_SKILL）经 resume_spawn_nested 续跑」。

运行：
    PYTHONPATH=src uv run python examples/multi_expert_consult/nested_hitl_demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import Resume
from taifeng.telemetry import attach_console_sink
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

# 编排器（entry）：把嵌套专科列进白名单后 spawn。
_ORCH = """---
name: orchestrator
description: MDT 编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [nested-expert]
tool_names: [spawn_skill, await_skills, join_skill, kill_skill]
max_call_depth: 4
---
# MDT 编排器 ORCH_MARK
spawn 嵌套专科做并发会诊。
"""

# 专科（composite）：top turn 经 call_skill 编排一个子步骤，再据其结论给最终结论。
_EXPERT = """---
name: nested-expert
description: 嵌套专科（top 编排子 skill）
version: 1.0.0
type: composite
model: mock-model
child_skills: [nested-step]
max_call_depth: 3
---
# 嵌套专科 EXPERT_MARK
先 call_skill 调用 nested-step 采集补充信息，拿到结论后给最终诊断。
"""

# 子步骤（leaf）：执行中 request_user_input 向用户补料（真实 DATA 挂起所在层）。
_STEP = """---
name: nested-step
description: 子步骤（请求用户补料）
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 子步骤 STEP_MARK
先 request_user_input 补问，再据答复给结论。
"""


def _client() -> RoutingSimClient:
    """按 body 标记路由：专科两轮（call_skill→最终），子步骤两轮（补问挂起→结论）。"""
    return RoutingSimClient(routes={
        "EXPERT_MARK": [
            SimTurn(text="专科编排：先调用子步骤采集信息。", tool_calls=[
                {"id": "call_step", "name": "call_skill",
                 "arguments": '{"skill_id": "nested-step", "args": {}}'}]),
            SimTurn(text="专科最终诊断 EXPERT_DONE"),
        ],
        "STEP_MARK": [
            SimTurn(text="子步骤需要补充信息。", tool_calls=[
                {"id": "step_ask", "name": "request_user_input",
                 "arguments": '{"prompt": "请补充近期血糖值"}'}]),
            SimTurn(text="子步骤结论 STEP_DONE"),
        ],
    })


async def main() -> None:
    """spawn 嵌套专科 → 子 skill HITL 挂起（嵌套 CHILD_SKILL）→ Resume → 续跑链跑完。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        for sub, body in (
            ("orchestrator", _ORCH), ("nested-expert", _EXPERT),
            ("nested-step", _STEP),
        ):
            (skills / sub).mkdir(parents=True)
            (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=root / "threads",
            model_client=_client(), compressors=[],
            extra_tools=[make_request_user_input_tool()],
        )
        engine = await pool.get_or_create(
            session_id="nested-mdt", entry_skill_id="orchestrator")
        sink_task = attach_console_sink(engine, color=True)

        events: list = []
        collector = asyncio.create_task(_collect(engine, events))

        # 1. spawn 嵌套专科（绕开编排器 LLM turn，聚焦续跑链）
        sp = await engine.spawn_skill(
            skill_id="nested-expert", args={}, reason="嵌套专科会诊")
        hid, child_tid = sp["handle_id"], sp["child_thread_id"]

        # 2. 等专科句柄因子 skill 的 CHILD_SKILL 而挂起
        await _wait(lambda: engine.spawn_status([hid])[hid]["status"] == "suspended")

        # 3. 取 leaf 子步骤的真实 DATA 挂起 request_id（submission_id == spawn 子 thread）
        req_id = _leaf_data_req(events)

        print("\n" + "=" * 64)
        print("[嵌套挂起] 专科句柄 status=suspended（spawn 子 thread CHILD_SKILL）")
        print(f"[嵌套挂起] 真实 DATA 挂起埋在 leaf 子步骤 request_id={req_id}")
        print("=" * 64)

        # 4. Resume(spawn 子 thread, leaf request_id) → 嵌套续跑链
        await engine.submit(Resume(
            thread_id=child_tid,
            resolutions={req_id: {"answer": "空腹血糖 6.3 mmol/L"}}))

        # 5. 续跑链应让专科跑到终态
        done = await _wait(
            lambda: engine.spawn_status([hid])[hid]["status"] == "done")

        await asyncio.sleep(0.4)
        sink_task.cancel()
        collector.cancel()

        result = engine.spawn_status([hid])[hid].get("result")
        print("\n" + "=" * 64)
        print(f"嵌套续跑链完成 = {done}  专科结论 = {result!r}")
        print(f"==> 嵌套 CHILD_SKILL 错峰 HITL 续跑"
              f"{'确证 ✅' if done and result == '专科最终诊断 EXPERT_DONE' else '未完成 ❌'}")
        print("=" * 64)
        await pool.close()


async def _collect(engine: object, events: list) -> None:
    """旁路订阅全事件（取 leaf 挂起 request_id + 收尾统计）。"""
    async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
        events.append(ev)


def _leaf_data_req(events: list) -> str | None:
    """从已观测事件取 leaf 子步骤 DATA 挂起的 request_id。"""
    for ev in events:
        if ev.msg.kind != "turn_suspended":
            continue
        for p in ev.msg.data.get("pending") or []:
            if p.get("reason") == "data":
                return p["request_id"]
    return None


async def _wait(cond, tries: int = 400) -> bool:
    """轮询等待条件成立（每次 10ms），等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


if __name__ == "__main__":
    asyncio.run(main())
