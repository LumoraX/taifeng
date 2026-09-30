"""执行归属标识（ADR 0109）：工具与脚本执行器能把一次执行归到具体的会话与那一轮提交。

三条执行路径——turn 内正常派发、审批通过后在 engine 层重跑、子 thread 上重跑——给工具的
``ToolContext.extras`` 都带 ``submission_id`` 与 ``session_id``；``run_script`` 把线程与会话标识
交给 ``ScriptExecutor``。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import Resume
from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import last_turn_terminal
from tests.test_child_suspend_resume import _AllEventsRecorder, _build_skills, _routing_client
from tests.test_suspend import _build_suspend_skill, _drain_until_terminal

if TYPE_CHECKING:
    from pathlib import Path


def _recording_danger(seen: list[dict[str, Any]]) -> ToolSpec:
    """需审批的工具：每次 handler 被调用都记下 extras 里的归属标识。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        seen.append({
            "thread_id": ctx.thread_id,
            "submission_id": ctx.extras.get("submission_id"),
            "session_id": ctx.extras.get("session_id"),
        })
        await ctx.extras["permission_policy"].check(PermissionRequest.for_tool_call(
            "danger", args, thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("root",), extra_metadata={"call_id": ctx.call_id},
        ))
        return ToolResult.ok("danger executed")

    return ToolSpec(
        name="danger", description="需审批的工具",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler, parallel_safe=True,
    )


async def _pending_request_id(pool: taifeng.EnginePool, thread_id: str) -> str:
    items = [it async for it in await pool.store.load_thread(thread_id)]
    (suspension,) = [it for it in items if it.kind == "suspension"]
    return SuspensionRecord.from_item(suspension).pending[0].request_id


async def test_dispatch_and_approved_rerun_both_carry_submission_and_session(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """正常派发带发起那一轮的 submission id；审批通过后的重跑带 ``Resume`` 那一轮的。"""
    seen: list[dict[str, Any]] = []
    _build_suspend_skill(skills_dir)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[],
        model_client=SimClient(turns=[
            SimTurn(text="calling", tool_calls=[{"id": "c1", "name": "danger", "arguments": "{}"}]),
            SimTurn(text="done"),
        ]),
        extra_tools=[_recording_danger(seen)],
        permission_policy=PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()),
    )
    engine = await pool.get_or_create(session_id="ses-identity", entry_skill_id="suspend-skill")

    first = await engine.submit(taifeng.UserMessage(text="go"))
    assert (await _drain_until_terminal(engine, first))[-1].msg.kind == "turn_suspended"
    request_id = await _pending_request_id(pool, engine.thread_id)
    resume = await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={request_id: {"granted": True}},
    ))
    assert (await _drain_until_terminal(engine, resume))[-1].msg.kind == "turn_completed"
    await pool.close()

    assert seen == [
        {"thread_id": engine.thread_id, "submission_id": first, "session_id": "ses-identity"},
        {"thread_id": engine.thread_id, "submission_id": resume, "session_id": "ses-identity"},
    ]


async def test_rerun_on_a_child_thread_carries_submission_and_session(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """子 skill 里挂起的调用在子 thread 上重跑时同样带归属标识。"""
    seen: list[dict[str, Any]] = []
    pool = await taifeng.EnginePool.create(
        skills_dir=_build_skills(tmp_path), threads_dir=threads_dir, compressors=[],
        model_client=_routing_client(), extra_tools=[_recording_danger(seen)],
        permission_policy=PermissionPolicy(
            default_mode="ask", prompter=SuspendingPrompter(),
            rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        ),
    )
    engine = await pool.get_or_create(session_id="ses-child", entry_skill_id="parent-orch")
    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)

    first = await engine.submit(taifeng.UserMessage(text="go"))
    events = await recorder.wait_terminal(first)
    assert last_turn_terminal(events) == "turn_suspended"
    child_thread_id = next(e for e in events if e.msg.kind == "turn_suspended").msg.data["thread_id"]
    assert child_thread_id != engine.thread_id
    request_id = await _pending_request_id(pool, child_thread_id)
    resume = await engine.submit(Resume(
        thread_id=child_thread_id, resolutions={request_id: {"granted": True}},
    ))
    await recorder.wait_terminal(resume)
    await pool.close()

    assert seen == [
        # call_skill 的子 turn 与父同属一次提交、同一个会话
        {"thread_id": child_thread_id, "submission_id": first, "session_id": "ses-child"},
        {"thread_id": child_thread_id, "submission_id": resume, "session_id": "ses-child"},
    ]


_SCRIPT_SKILL = """---
name: data-skill
description: 数据 skill
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [helper]
tool_names: [run_script]
scripts:
  - name: greet
    path: scripts/greet.sh
    language: shell
    timeout_seconds: 5
    description: 打个招呼
---
# data-skill body
"""

_HELPER_SKILL = """---
name: helper
description: 占位
version: 1.0.0
type: atomic
---
# helper
"""


class _CapturingExecutor:
    """记录收到的 ``ScriptInvocation``，不真的执行脚本。"""

    def __init__(self) -> None:
        self.invocations: list[taifeng.ScriptInvocation] = []

    async def execute(self, invocation: taifeng.ScriptInvocation) -> taifeng.ScriptResult:
        self.invocations.append(invocation)
        return taifeng.ScriptResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=1,
            truncated=False, is_timeout=False, killed=False,
        )


async def test_script_invocation_names_its_thread_and_session(tmp_path: Path) -> None:
    """``ScriptExecutor`` 拿到这次脚本所属的线程与会话，可据此按会话隔离工作区。"""
    skills = tmp_path / "skills"
    for name, body in (("data-skill", _SCRIPT_SKILL), ("helper", _HELPER_SKILL)):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(body, encoding="utf-8")
    script = skills / "data-skill" / "scripts" / "greet.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    executor = _CapturingExecutor()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads", compressors=[],
        model_client=SimClient(turns=[
            SimTurn(text="跑脚本", tool_calls=[{
                "id": "c1", "name": "run_script",
                "arguments": '{"skill_id": "data-skill", "script_name": "greet", "args": {}}',
            }]),
            SimTurn(text="完成"),
        ]),
        script_executors={"shell": executor},
    )
    engine = await pool.get_or_create(session_id="ses-script", entry_skill_id="data-skill")
    submission = await engine.submit(taifeng.UserMessage(text="run"))
    assert (await _drain_until_terminal(engine, submission))[-1].msg.kind == "turn_completed"
    await pool.close()

    (invocation,) = executor.invocations
    assert invocation.thread_id == engine.thread_id
    assert invocation.session_id == "ses-script"
    assert invocation.submission_id == submission
    assert invocation.call_id == "c1"


def test_script_invocation_identity_fields_are_optional() -> None:
    """已有的执行器与调用方不受影响：标识字段都有默认值。"""
    from taifeng.loop.cancellation import CancellationToken

    descriptor = taifeng.ScriptDescriptor(
        skill_id="s", name="n", path="/tmp/x.sh",  # noqa: S108  # 只构造描述符，不触碰文件
        language="shell", description="d",
    )
    invocation = taifeng.ScriptInvocation(descriptor=descriptor, args={}, cancel=CancellationToken())
    assert (invocation.thread_id, invocation.session_id) == (None, None)
    assert (invocation.submission_id, invocation.call_id) == (None, None)
