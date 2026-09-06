"""子 thread 续跑 turn 必须登记 `_pending`，`CancelTurn(resume_sub_id)` 才能触达（R4）。

复用 tests/test_child_suspend_resume.py 的三 skill 搭建：父派子 → 子内 danger 挂起 →
Resume(子 thread) → 子续跑（SimTurn 放慢 1s）→ 立刻 CancelTurn(resume_sub)。
此前 `_run_thread_turn` 不登记 `_pending`，CancelTurn 找不到目标只能 no-op，续跑
只能等自然结束。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import CancelTurn, Resume
from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.suspend.record import SuspensionRecord
from tests.conftest import wait_for_condition
from tests.test_child_suspend_resume import (
    _AllEventsRecorder,
    _build_skills,
    _gated_danger_tool,
)

if TYPE_CHECKING:
    from pathlib import Path


def _slow_child_client() -> RoutingSimClient:
    """与 _routing_client 同路由，但子续跑那一轮放慢 1s 给 CancelTurn 留窗口。"""
    return RoutingSimClient(routes={
        "ENTRY_MARK": [
            SimTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id": "child-worker", "reason": "do work"}'},
            ]),
            SimTurn(text="父编排完成。"),
        ],
        "CHILD_MARK": [
            SimTurn(text="子调用 danger", tool_calls=[
                {"id": "call_d1", "name": "danger", "arguments": "{}"},
            ]),
            SimTurn(text="子工作完成 CHILD_DONE_MARK", delay_seconds=1.0),
        ],
    })


async def test_cancel_turn_reaches_child_thread_resume(tmp_path: Path, threads_dir: Path) -> None:
    skills = _build_skills(tmp_path)
    policy = PermissionPolicy(
        default_mode="ask",
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        prompter=SuspendingPrompter(),
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=_slow_child_client(), compressors=[],
        extra_tools=[await _gated_danger_tool()], permission_policy=policy,
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="parent-orch")
        recorder = _AllEventsRecorder(engine)
        # create_task 只是排期：确认订阅真登记上再提交，否则首批事件整段丢失
        await wait_for_condition(
            lambda: recorder.registered(engine), message="firehose 订阅未能登记"
        )

        sub_id = await engine.submit(taifeng.UserMessage(text="go"))
        events1 = await recorder.wait_terminal(sub_id)
        suspend_ev = next(ev for ev in events1 if ev.msg.kind == "turn_suspended")
        child_tid = suspend_ev.msg.data["thread_id"]
        items = [it async for it in await pool.store.load_thread(child_tid)]
        rec = SuspensionRecord.from_item(next(it for it in items if it.kind == "suspension"))
        req_id = rec.pending[0].request_id

        resume_sub = await engine.submit(Resume(
            thread_id=child_tid, resolutions={req_id: {"granted": True}},
        ))
        # 前提是「CancelTurn 到达时子续跑正在采样」。原来 sleep(0.3) 赌这一点：
        # 机器一慢睡醒时子 turn 可能还没起飞（或已跑完），前提消失、症状却是下游
        # wait_terminal 超时。改成等子续跑真的 turn_started。
        await wait_for_condition(
            lambda: recorder.seen(
                lambda e: e.submission_id == resume_sub
                and e.msg.kind == "turn_started"
                and not e.msg.data.get("is_root")
            ),
            message="子续跑 turn 未起飞",
        )
        await engine.submit(CancelTurn(submission_id=resume_sub))

        events2 = await recorder.wait_terminal(resume_sub)
        # 内核语义：token 取消的 turn 以 turn_completed{end_reason=cancelled} 收尾。
        # 子续跑 turn 是 resume 后第一个 turn_completed（is_root=False）。
        child_done = next(
            ev for ev in events2
            if ev.msg.kind == "turn_completed" and not ev.msg.data.get("is_root")
        )
        assert child_done.msg.data["end_reason"] == "cancelled", (
            f"CancelTurn 未触达子续跑：{[e.msg.kind for e in events2]}"
        )

        child_items = [it async for it in await pool.store.load_thread(child_tid)]
        blob = " ".join(str(it.payload) for it in child_items)
        assert "CHILD_DONE_MARK" not in blob, "被取消的续跑不应落完成输出"
        # wave2b D5:链取消即停——父 / 根不再重跑,Resume submission 以
        # turn_failed{cancelled} 终结(完整解链断言见 test_spawn_redrive_truth.py)。
        root_term = next(
            ev for ev in events2
            if ev.msg.kind in ("turn_completed", "turn_failed")
            and ev.msg.data.get("is_root"))
        assert root_term.msg.kind == "turn_failed", "链取消后根不得再采样完成"
        assert root_term.msg.data.get("kind") == "cancelled"
    finally:
        await pool.close()
