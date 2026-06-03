"""子线程（call_skill 派发的子 skill 内）HITL 挂起 → Resume 续跑回传父 call_skill。

复现并固化核心引擎缺陷：当父 skill 经 call_skill 派发子 skill，子 skill 内
触发 request_user_input / permission ask 挂起时，挂起记录落在【子 thread】，
turn_suspended 事件携带的是子 thread_id。而 engine 既往的 _handle_resume 只在
【根 thread】的 self._history 找活跃挂起，命中子挂起 → no_active_suspension 被拒。

本测试断言：
  1. 子 skill 内挂起后，turn_suspended 携带子 thread_id（≠ 根 thread_id）；
  2. 提交 Resume(thread_id=<子 thread_id>, resolutions=...) → 子挂起被定位、resolve；
  3. 子 turn 续跑完成，其结果回传父 call_skill → 父 turn 续跑 → 整个 submission
     以根 turn_completed 收尾（item (d)：续跑回传父调用栈）。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from pathlib import Path

# 父 entry skill：body 含 ENTRY_MARK，child_skills 声明子 skill，自身不挂工具
_PARENT_SKILL = """---
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

# 子 skill：body 含 CHILD_MARK，声明需审批的 danger 工具。composite 须有 child_skills，
# 故挂一个 leaf 子 skill（仅为满足校验，本用例不实际派发它）。
_CHILD_SKILL = """---
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

# leaf：仅用于满足 child-worker 的 child_skills 校验
_LEAF_SKILL = """---
name: leaf-noop
description: 叶子占位
version: 1.0.0
type: atomic
---
# 叶子占位
不做实际工作。
"""


def _build_skills(tmp_path: Path) -> Path:
    """内联写出 parent-orch(entry) + child-worker + leaf-noop 三个 skill，返回 skills 目录。"""
    skills = tmp_path / "child_resume_skills"
    for sub, body in (
        ("parent-orch", _PARENT_SKILL),
        ("child-worker", _CHILD_SKILL),
        ("leaf-noop", _LEAF_SKILL),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


async def _gated_danger_tool():
    """构造一个 ask 门控工具：门控时透传 ctx.call_id 进 PermissionRequest.metadata。

    与 test_suspend.py 的 `_gated_tool_with_call_id_factory` 同构 —— SuspendingPrompter
    据 metadata.call_id 把 related_call_id 填进 PendingRequest，供 resume 定位被挂起
    的 tool。resume 二次放行（policy 已不再 ask）时返回真实结果用于回填 output。
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
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


def _routing_client():
    """RoutingMockClient：父首轮 call_skill 派子；子首轮 danger（挂起）；子续跑纯文本完成。

    路由标记取各 skill body 唯一标记（ENTRY_MARK / CHILD_MARK），父子互不串扰。
      - ENTRY_MARK 第 1 次命中：吐 call_skill(child-worker)；
        第 2 次命中（子完成回传后父续采样）：纯文本 → 父 turn 完成。
      - CHILD_MARK 第 1 次命中：吐 danger tool call（触发审批挂起）；
        第 2 次命中（resume 后子续采样）：纯文本 → 子 turn 完成。
    """
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient

    return RoutingMockClient(routes={
        "ENTRY_MARK": [
            MockTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id": "child-worker", "reason": "do work"}'},
            ]),
            MockTurn(text="父编排完成。"),
        ],
        "CHILD_MARK": [
            MockTurn(text="子调用 danger", tool_calls=[
                {"id": "call_d1", "name": "danger", "arguments": "{}"},
            ]),
            MockTurn(text="子工作完成 CHILD_DONE_MARK"),
        ],
    })


class _AllEventsRecorder:
    """后台 subscribe_all 收集器 —— submit 前启动，避免内联 resume 抢跑丢首批事件。

    本场景一次 submission 跨【子 turn + 根 turn】两个 turn，各自 emit turn_completed；
    subscribe(sub_id) 会在首个 turn_completed（子，is_root=False）即返回，拿不到根完成。
    故统一用 subscribe_all 全量收集，再按 (submission_id, 终结判据) 等待目标 submission
    的根终结事件（turn_suspended 任意层 / turn_completed|turn_failed 且 is_root）。
    """

    def __init__(self, engine) -> None:
        self._events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self._events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    async def wait_terminal(self, sub_id: str, *, timeout_s: float = 8.0) -> list:
        """轮询直到 sub_id 的根终结事件出现，返回该 submission 的全部已收事件。"""
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
async def test_resume_child_thread_suspension_continues_parent(tmp_path: Path, threads_dir):
    """子 skill 内 permission 挂起 → Resume(子 thread_id) → 子续跑 + 回传父 → 根完成。"""
    import taifeng
    from taifeng.loop.submission import Resume
    from taifeng.permission.types import (
        PermissionPolicy,
        PermissionRule,
        SuspendingPrompter,
    )

    skills = _build_skills(tmp_path)
    gated_tool = await _gated_danger_tool()
    # 关键：放行 skill_dispatch（call_skill 不挂起），仅 default ask 门控子 skill 内的
    # danger（scope=tool_use）。这样挂起点落在【子 thread】而非父的 call_skill 派发 gate。
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
        model_client=_routing_client(),
        compressors=[],
        extra_tools=[gated_tool],
        permission_policy=policy,
    )
    engine = await pool.get_or_create(
        session_id="child-resume-e2e", entry_skill_id="parent-orch",
    )
    root_thread_id = engine.thread_id

    # submit 前启动全量事件收集器（避免内联/异步 resume 抢跑丢首批事件）
    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    # === 第一阶段：父派子，子内 danger 挂起 ===
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    kinds1 = [ev.msg.kind for ev in events1]
    assert events1[-1].msg.kind == "turn_suspended", f"应以挂起收尾，实得 {kinds1}"

    # 挂起事件携带的是【子 thread_id】，且 ≠ 根 thread_id
    suspend_ev = next(ev for ev in events1 if ev.msg.kind == "turn_suspended")
    child_thread_id = suspend_ev.msg.data["thread_id"]
    assert child_thread_id != root_thread_id, "子挂起的 thread_id 必须是子 thread"

    # 从子 thread 持久化的 suspension record 取 request_id（续跑入参）
    child_items = [it async for it in await pool.store.load_thread(child_thread_id)]
    suspension_items = [it for it in child_items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "子 thread 应落且仅落一条 suspension"
    rec = SuspensionRecord.from_item(suspension_items[0])
    req_id = rec.pending[0].request_id
    assert rec.pending[0].related_call_id == "call_d1"

    # === 第二阶段：Resume(子 thread_id) 续跑 ===
    resume_sub = await engine.submit(Resume(
        thread_id=child_thread_id,
        resolutions={req_id: {"granted": True}},
    ))
    events2 = await recorder.wait_terminal(resume_sub)
    kinds2 = [ev.msg.kind for ev in events2]

    await pool.close()

    # (a)(b) 子挂起被定位并 resolve
    assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
    # (c)(d) 续跑回传父 → 整个 submission 以根 turn_completed 收尾
    root_completed = [
        ev for ev in events2
        if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
    ]
    assert root_completed, f"续跑应回传父并以根 turn_completed 收尾，实得 {kinds2}"

    # 子 thread 续跑后的输出（CHILD_DONE_MARK）必须出现在子 thread 历史里
    child_items2 = [it async for it in await pool.store.load_thread(child_thread_id)]
    blob = " ".join(str(it.payload) for it in child_items2)
    assert "CHILD_DONE_MARK" in blob, "子 thread 续跑后的输出必须落盘"
    # 被挂起的 call_d1 续跑后补回 function_call_output（gap 补齐）
    fco_ids = {
        it.payload.get("call_id") for it in child_items2
        if it.kind == "function_call_output"
    }
    assert "call_d1" in fco_ids, "被批准的挂起 call 必须补回 function_call_output"
