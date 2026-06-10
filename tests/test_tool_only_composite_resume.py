"""tool-only composite 子 skill 内 request_user_input 挂起 → Resume 续跑回传父 → 根完成。

本特性（ADR 0013）的端到端证据：子 skill 是 tool-only composite —— 只声明
tool_names: [request_user_input]、无 child_skills（对比 test_child_suspend_resume.py
里为过校验而捏的 leaf-noop dummy 子 skill，本测试证明那已不再需要）。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from pathlib import Path

# 父 entry：LLM 驱动，派发子 skill，自身不挂工具
_PARENT_SKILL = """---
name: parent-flow
description: 父流程
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [intake-analyzer]
tool_names: []
max_call_depth: 3
---
# 父流程 PARENT_MARK
派发子 skill 完成分析。
"""

# 子 skill：tool-only composite —— 仅 tool_names、无 child_skills（本特性核心形态）
_CHILD_SKILL = """---
name: intake-analyzer
description: 采集分析子单元
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 采集分析 CHILD_MARK
缺数据时调 request_user_input 向用户采集，补齐后给结论。
"""


def _build_skills(tmp_path: Path) -> Path:
    """内联写出 parent-flow(entry) + intake-analyzer 两个 skill（无 dummy 叶子）。"""
    skills = tmp_path / "tool_only_skills"
    for sub, body in (
        ("parent-flow", _PARENT_SKILL),
        ("intake-analyzer", _CHILD_SKILL),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _routing_client():
    """父首轮 call_skill 派子；子首轮 request_user_input（挂起）；各自第 2 轮纯文本完成。"""
    from taifeng.llm.providers import SimTurn
    from taifeng.llm.providers.sim import RoutingSimClient

    return RoutingSimClient(routes={
        "PARENT_MARK": [
            SimTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id": "intake-analyzer", "reason": "analyze"}'},
            ]),
            SimTurn(text="父流程完成。"),
        ],
        "CHILD_MARK": [
            SimTurn(text="子向用户采集", tool_calls=[
                {"id": "call_rui1", "name": "request_user_input",
                 "arguments": '{"prompt": "请补充近三月体检报告"}'},
            ]),
            SimTurn(text="子分析完成 CHILD_DONE_MARK"),
        ],
    })


class _AllEventsRecorder:
    """后台 subscribe_all 收集器 —— submit 前启动，按 (submission_id, 终结判据) 等目标终结。"""

    def __init__(self, engine) -> None:
        self._events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self._events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    async def wait_terminal(self, sub_id: str, *, timeout_s: float = 8.0) -> list:
        async def _poll() -> list:
            while True:
                got = [e for e in self._events if e.submission_id == sub_id]
                for e in got:
                    k = e.msg.kind
                    if k == "turn_suspended":
                        return got
                    if k in ("turn_completed", "turn_failed") and e.msg.data.get("is_root"):
                        return got
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout=timeout_s)


@pytest.mark.asyncio
async def test_tool_only_composite_child_suspend_resume(tmp_path: Path, threads_dir):
    """tool-only composite 子 skill request_user_input 挂起 → Resume(子 thread) → 根完成。"""
    import taifeng
    from taifeng.loop.submission import Resume
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    skills = _build_skills(tmp_path)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=threads_dir,
        model_client=_routing_client(),
        compressors=[],
        extra_tools=[make_request_user_input_tool()],  # request_user_input 非默认注册
    )
    engine = await pool.get_or_create(
        session_id="tool-only-e2e", entry_skill_id="parent-flow",
    )
    root_thread_id = engine.thread_id

    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    # === 第一阶段：父派子，子内 request_user_input 挂起 ===
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    assert events1[-1].msg.kind == "turn_suspended", \
        f"应以挂起收尾，实得 {[e.msg.kind for e in events1]}"

    suspend_ev = next(ev for ev in events1 if ev.msg.kind == "turn_suspended")
    child_thread_id = suspend_ev.msg.data["thread_id"]
    assert child_thread_id != root_thread_id, "子挂起的 thread_id 必须是子 thread"

    # 从子 thread suspension record 取 request_id
    # （DATA 挂起：request_id == related_call_id == call_rui1）
    child_items = [it async for it in await pool.store.load_thread(child_thread_id)]
    suspension_items = [it for it in child_items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "子 thread 应落且仅落一条 suspension"
    rec = SuspensionRecord.from_item(suspension_items[0])
    req_id = rec.pending[0].request_id
    assert rec.pending[0].related_call_id == "call_rui1"

    # === 第二阶段：Resume(子 thread) 回填表单答案 → 续跑 ===
    resume_sub = await engine.submit(Resume(
        thread_id=child_thread_id,
        resolutions={req_id: {"report": "已上传"}},
    ))
    events2 = await recorder.wait_terminal(resume_sub)
    kinds2 = [ev.msg.kind for ev in events2]

    await pool.close()

    # (a) 子挂起被定位并 resolve
    assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
    # (b) 续跑回传父 → 整个 submission 以根 turn_completed 收尾
    root_completed = [
        ev for ev in events2
        if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
    ]
    assert root_completed, f"续跑应回传父并以根 turn_completed 收尾，实得 {kinds2}"

    # (c) 子 thread 续跑输出落盘 + 被挂起 call 补回 function_call_output
    child_items2 = [it async for it in await pool.store.load_thread(child_thread_id)]
    blob = " ".join(str(it.payload) for it in child_items2)
    assert "CHILD_DONE_MARK" in blob, "子 thread 续跑后的输出必须落盘"
    fco_ids = {
        it.payload.get("call_id") for it in child_items2
        if it.kind == "function_call_output"
    }
    assert "call_rui1" in fco_ids, "被 resolve 的挂起 call 必须补回 function_call_output"
