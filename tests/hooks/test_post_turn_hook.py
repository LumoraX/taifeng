"""post-turn-hook 集成测试。

覆盖 spec ``post-turn-hook`` 的 Requirement:
    - root turn 真终态(completed/failed)同步触发 post_turn(审计型,不可否决)
    - 挂起(suspended)不触发;resume 跑到真终态才触发(关键坑 D2)
    - 仅 root turn 触发(子 turn 不触发)
    - 审计型:deny / 钩子异常都不改变已终结的 turn
    - 入参取自 TurnOutcome;ctx.extras 携带 cancel token(R4)

post_turn 在 turn_completed **之后**触发,而 ``subscribe(submission_id)`` 在
turn_completed 处即关闭订阅 —— 故 post_turn_hook_fired(R3 遥测)只在
``subscribe_all`` 火炬流可见;handler 本身是同步调用,用 call_log 验证。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------
# 公共辅助
# --------------------------------------------------------------------


async def _start_collector(
    engine: taifeng.AgentEngine,
) -> tuple[list[str], asyncio.Task]:
    """启动 subscribe_all 收集器(必须在 submit 之前调用以免漏事件)。"""
    seen: list[str] = []

    async def _collect() -> None:
        async for ev in engine.subscribe_all():
            seen.append(ev.msg.kind)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return seen, task


async def _stop_collector(
    task: asyncio.Task, settle_seconds: float = 0.3,
) -> None:
    """等待事件落定后停止收集器。"""
    await asyncio.sleep(settle_seconds)
    task.cancel()
    with suppress(asyncio.CancelledError, BaseException):
        await task


def _post_turn_recorder() -> tuple[list[dict], object]:
    """返回 (call_log, handler):handler 把每次 post_turn 入参 + 是否带 cancel 记入 log。"""
    call_log: list[dict] = []

    async def handler(hook, ctx) -> HookDecision:
        call_log.append({
            "end_reason": hook.end_reason,
            "success": hook.success,
            "final_text": hook.final_text,
            "iteration": hook.iteration,
            "has_cancel": "cancel" in ctx.extras,
        })
        return HookDecision.ok()

    return call_log, handler


async def _danger_tool():
    """需审批的危险工具:ask 模式下 SuspendingPrompter 抛 SuspendSignal → turn 挂起。"""
    from taifeng.permission.types import PermissionRequest
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    async def handler(args, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        # extra_metadata={"call_id":...} 使 pending 带 related_call_id —— resume
        # 放行时据此定位「该执行哪个挂起 tool」(否则 resolve 被拒)。
        req = PermissionRequest.for_tool_call(
            "danger",
            args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("root",),
            extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        return ToolResult.ok("danger executed")

    return ToolSpec(
        name="danger",
        description="需审批的危险工具(测试用)",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


def _build_suspend_skill(skills_dir: Path) -> None:
    """写入声明 danger 工具的 entry composite skill。"""
    skill_md = """---
name: suspend-skill
description: suspend entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: [danger]
max_call_depth: 2
---
# Suspend
"""
    (skills_dir / "suspend-skill").mkdir()
    (skills_dir / "suspend-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")


# --------------------------------------------------------------------
# 1) completed turn 触发 post_turn + 入参 + cancel token + 事件
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_fires_on_completed(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """正常完成的 root turn 触发一次 post_turn,入参取自 outcome,ctx 带 cancel。"""
    client = SimClient(turns=[SimTurn(
        text="ok",
        usage=TokenUsage(input_tokens=10, output_tokens=2),
    )])
    reg = HookRegistry()
    call_log, handler = _post_turn_recorder()
    reg.register("post_turn", handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-ok", entry_skill_id="code-reviewer",
    )
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="问个问题"))
    await _stop_collector(task)
    await pool.close()

    # R3 事件在 subscribe_all 可见,且在 turn_completed 之后(turn 收尾后才审计)
    assert "turn_completed" in seen
    assert "post_turn_hook_fired" in seen
    assert seen.index("turn_completed") < seen.index("post_turn_hook_fired")
    # handler 同步触发恰一次,入参取自 TurnOutcome
    assert len(call_log) == 1, call_log
    rec = call_log[0]
    assert rec["end_reason"] == "completed"
    assert rec["success"] is True
    assert rec["final_text"] == "ok"
    assert rec["iteration"] == 0  # 第一个 turn = index 0(必须用 +1 之前的值)
    assert rec["has_cancel"] is True  # R4:ctx.extras 带 cancel token


# --------------------------------------------------------------------
# 2) 未注册 post_turn handler → 不触发事件(零开销路径)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_skipped_when_no_handler(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """无 post_turn handler 时不 emit post_turn_hook_fired(常见路径零开销)。"""
    client = SimClient(turns=[SimTurn(
        text="ok", usage=TokenUsage(input_tokens=10, output_tokens=2),
    )])
    reg = HookRegistry()  # 不注册任何 post_turn

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-none", entry_skill_id="code-reviewer",
    )
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="问题"))
    await _stop_collector(task)
    await pool.close()

    assert "turn_completed" in seen
    assert "post_turn_hook_fired" not in seen


# --------------------------------------------------------------------
# 3) 挂起不触发;resume 跑到终态才触发(关键坑 D2)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_not_fired_on_suspend_then_fired_on_resume(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """suspended 不触发 post_turn;Resume 续跑到 completed 才触发一次。"""
    from taifeng.loop.submission import Resume
    from taifeng.permission.types import PermissionPolicy, SuspendingPrompter
    from taifeng.suspend.record import SuspensionRecord

    gated = await _danger_tool()
    _build_suspend_skill(skills_dir)
    client = SimClient(turns=[
        SimTurn(
            text="calling danger",
            tool_calls=[{"id": "call_d1", "name": "danger", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
        SimTurn(
            text="approved and done",
            usage=TokenUsage(input_tokens=8, output_tokens=4),
        ),
    ])
    reg = HookRegistry()
    call_log, handler = _post_turn_recorder()
    reg.register("post_turn", handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[gated], hooks=HookRunner(reg),
        permission_policy=PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-suspend", entry_skill_id="suspend-skill",
    )

    # 第一轮:挂起 —— post_turn 此刻 MUST NOT 触发
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="go"))
    await _stop_collector(task)
    assert "turn_suspended" in seen
    assert "post_turn_hook_fired" not in seen
    assert call_log == [], f"挂起不应触发 post_turn,但 log={call_log}"

    # 取 request_id
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    rec = SuspensionRecord.from_item(
        [it for it in items if it.kind == "suspension"][0]
    )
    req_id = rec.pending[0].request_id

    # 第二轮:Resume 续跑到 completed —— post_turn 此刻才触发一次
    seen2, task2 = await _start_collector(engine)
    await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"granted": True}},
    ))
    await _stop_collector(task2)
    await pool.close()

    assert "turn_completed" in seen2
    assert "post_turn_hook_fired" in seen2
    assert len(call_log) == 1, f"resume 完成应恰触发一次,log={call_log}"
    assert call_log[0]["end_reason"] == "completed"


# --------------------------------------------------------------------
# 4) 审计型:deny 不改变已完成的 turn(且 handler 确被触发)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_deny_does_not_affect_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """post_turn 返回 deny 不回滚 turn —— 审计型语义;handler 仍被触发。"""
    client = SimClient(turns=[SimTurn(
        text="ok", usage=TokenUsage(input_tokens=10, output_tokens=2),
    )])
    reg = HookRegistry()
    reached: list[str] = []

    async def deny_handler(hook, ctx) -> HookDecision:
        reached.append(hook.end_reason)
        return HookDecision.deny("just auditing")

    reg.register("post_turn", deny_handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-deny", entry_skill_id="code-reviewer",
    )
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="问题"))
    await _stop_collector(task)
    await pool.close()

    # handler 被触发,但 deny 不产生 turn_failed —— turn 仍正常完成
    assert reached == ["completed"], reached
    assert "turn_completed" in seen
    assert "turn_failed" not in seen


# --------------------------------------------------------------------
# 5) 审计型:钩子异常被吞,不影响 turn
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_handler_exception_swallowed(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """post_turn handler 抛异常被吞掉,turn 仍正常完成(run_audit_only 语义)。"""
    client = SimClient(turns=[SimTurn(
        text="ok", usage=TokenUsage(input_tokens=10, output_tokens=2),
    )])
    reg = HookRegistry()
    reached: list[str] = []

    async def boom_handler(hook, ctx) -> HookDecision:
        reached.append(hook.end_reason)
        raise RuntimeError("hook boom")

    reg.register("post_turn", boom_handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-boom", entry_skill_id="code-reviewer",
    )
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="问题"))
    await _stop_collector(task)
    await pool.close()

    assert reached == ["completed"], reached  # handler 确被触发
    assert "turn_completed" in seen
    assert "turn_failed" not in seen


# --------------------------------------------------------------------
# 6) 仅 root turn 触发:子 turn(call_skill)不触发 root 级 post_turn
# --------------------------------------------------------------------


_SCOPE_PARENT = """---
name: scope-parent
description: 父编排
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [scope-child]
tool_names: []
max_call_depth: 3
---
# 父编排 PARENT_MARK
派发子 skill。
"""

_SCOPE_CHILD = """---
name: scope-child
description: 子工作
version: 1.0.0
type: atomic
---
# 子工作 CHILD_MARK
完成具体工作。
"""


@pytest.mark.asyncio
async def test_post_turn_not_fired_for_subturn(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """父 turn 经 call_skill 派子 turn;post_turn 只对 root 触发一次(子 turn 不触发)。"""
    from taifeng.llm.providers.sim import RoutingSimClient

    skills = tmp_path / "scope_skills"
    for sub, body in (("scope-parent", _SCOPE_PARENT), ("scope-child", _SCOPE_CHILD)):
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")

    # 父首轮 call_skill 派子;子完成;父续采样完成。按 body 唯一标记路由,父子不串扰。
    client = RoutingSimClient(routes={
        "PARENT_MARK": [
            SimTurn(text="派子", tool_calls=[
                {"id": "c1", "name": "call_skill",
                 "arguments": '{"skill_id": "scope-child", "reason": "work"}'},
            ]),
            SimTurn(text="父完成"),
        ],
        "CHILD_MARK": [SimTurn(text="子完成")],
    })
    reg = HookRegistry()
    call_log, handler = _post_turn_recorder()
    reg.register("post_turn", handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-scope", entry_skill_id="scope-parent",
    )
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="go"))
    await _stop_collector(task, settle_seconds=0.5)
    await pool.close()

    # 两个 turn 都跑了(父 + 子各一次 turn_completed),但 post_turn 只触发一次(root)
    assert seen.count("turn_completed") >= 2, seen
    assert len(call_log) == 1, f"post_turn 应只对 root 触发一次,log={call_log}"
    assert call_log[0]["iteration"] == 0  # root turn


# --------------------------------------------------------------------
# 7) 门控直测:cancelled / suspended 不触发,completed 触发
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_gate_by_end_reason(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """直测 _fire_post_turn_hook 门控:suspended/cancelled 跳过,completed 触发。"""
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.turn import TurnOutcome

    client = SimClient(turns=[])
    reg = HookRegistry()
    call_log, handler = _post_turn_recorder()
    reg.register("post_turn", handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-gate", entry_skill_id="code-reviewer",
    )

    def _outcome(end_reason: str, success: bool) -> TurnOutcome:
        return TurnOutcome(
            success=success, iterations=1, duration_ms=1,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            final_text="x", end_reason=end_reason,
        )

    cancel = CancellationToken(name="test")
    # suspended / cancelled → 门控跳过
    await engine._fire_post_turn_hook(  # noqa: SLF001
        "sub-s", _outcome("suspended", False), cancel, 0)
    await engine._fire_post_turn_hook(  # noqa: SLF001
        "sub-c", _outcome("cancelled", False), cancel, 0)
    assert call_log == [], f"suspended/cancelled 不应触发,log={call_log}"

    # completed → 触发
    await engine._fire_post_turn_hook(  # noqa: SLF001
        "sub-ok", _outcome("completed", True), cancel, 3)
    await pool.close()

    assert len(call_log) == 1
    assert call_log[0]["end_reason"] == "completed"
    assert call_log[0]["iteration"] == 3


# --------------------------------------------------------------------
# 8) 真实保证:post_turn 在状态回写之后触发(turn N 收尾的同步一步)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_turn_fires_after_state_writeback(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """post_turn 触发时,本 turn 的 assistant 输出已回写进 engine.history。

    这是 post_turn 真正成立的「顺序」保证:它是 **turn N 收尾的同步一步**——在
    history / cache_anchor 回写之后、turn N 自己的 task 结束之前触发。
    (注:引擎**不**串行化相邻 turn;要「下一 turn 启动前完成」的跨 turn 顺序,
    宿主须等 post_turn_hook_fired 再提交下一轮,而非等 turn_completed。)
    """
    client = SimClient(turns=[SimTurn(
        text="本轮结论 Z", usage=TokenUsage(input_tokens=10, output_tokens=3),
    )])
    reg = HookRegistry()
    holder: dict = {}
    seen_at_fire: list[list[str]] = []

    async def handler(hook, ctx) -> HookDecision:
        # 触发时刻读 engine.history:应已含本轮 assistant_message(回写已发生)
        snap = holder["engine"].history_snapshot()
        seen_at_fire.append([it.kind for it in snap])
        return HookDecision.ok()

    reg.register("post_turn", handler)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=HookRunner(reg),
    )
    engine = await pool.get_or_create(
        session_id="s-pt-writeback", entry_skill_id="code-reviewer",
    )
    holder["engine"] = engine
    seen, task = await _start_collector(engine)
    await engine.submit(taifeng.UserMessage(text="给个结论"))
    await _stop_collector(task)
    await pool.close()

    assert seen_at_fire, "post_turn 未触发"
    kinds = seen_at_fire[0]
    # 回写已发生:本轮 user_message 与 assistant_message 都已在 history
    assert "user_message" in kinds, kinds
    assert "assistant_message" in kinds, kinds
