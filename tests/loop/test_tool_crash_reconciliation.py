"""tool-crash-reconciliation —— 工具执行途中崩溃后的冷恢复分流。

崩溃用「store 里只有 write-ahead 意图、没有结果」来模拟：这正是 Chat 路径在工具
执行期间进程被杀时 transcript 的真实形态（function_call 要等执行完才成对落盘）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.models import assistant_message, tool_intent_item, user_message
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import Resume
from taifeng.loop.tool_recovery import find_dangling_calls
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.resolver import ResolveError, SuspensionResolver
from taifeng.tool.spec import ReconcileVerdict, ToolResult, ToolSpec
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def _tool(name: str, *, effect_kind: str = "pure", reconciliation: str = "none",
          reconcile: Any = None, calls: list[str] | None = None) -> ToolSpec:
    """构造一个记录调用次数的测试工具。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        if calls is not None:
            calls.append(name)
        return ToolResult.ok(f"{name} executed")

    return ToolSpec(
        name=name, description=name,
        input_schema={"type": "object", "properties": {}},
        handler=_handler, effect_kind=effect_kind,
        reconciliation=reconciliation, reconcile=reconcile)


async def _crashed_thread(pool, tool_name: str, *, session_id: str) -> str:
    """造一个「工具执行中崩溃」的 thread：user → assistant → tool_intent，无结果。"""
    engine = await pool.get_or_create(session_id=session_id, entry_skill_id="code-reviewer")
    tid = engine.thread_id
    for item in (
        user_message("请处理", thread_id=tid),
        assistant_message("", thread_id=tid, model="auto"),
        tool_intent_item("call-1", tool_name, '{"id": "A-17"}', thread_id=tid),
    ):
        await pool.store.append(item)
    await pool.release(session_id, force=True)
    return tid


async def _resume_pool(skills_dir: Path, threads_dir: Path, tools: list[ToolSpec],
                       turns: list[SimTurn] | None = None) -> Any:
    """以给定工具集起一个新 pool（模拟进程重启）。"""
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=list(turns or [])),
        compressors=[], extra_tools=tools)


def _kinds(engine) -> list[str]:
    return [it.kind for it in engine.history_snapshot()]


def _output(engine, call_id: str = "call-1"):
    outs = [it for it in engine.history_snapshot()
            if it.kind == "function_call_output" and it.payload.get("call_id") == call_id]
    assert len(outs) == 1, _kinds(engine)
    return outs[0]


async def test_pure_tool_intent_recovered_as_safe_to_retry(
    skills_dir: Path, threads_dir: Path
) -> None:
    """pure 工具：补 function_call + 回填「可安全重发」，不执行工具、不挂起。"""
    calls: list[str] = []
    setup = await _resume_pool(skills_dir, threads_dir, [_tool("lookup", calls=calls)])
    tid = await _crashed_thread(setup, "lookup", session_id="s0")
    await setup.close()

    pool = await _resume_pool(skills_dir, threads_dir, [_tool("lookup", calls=calls)])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)

    assert _kinds(engine)[-2:] == ["function_call", "function_call_output"]
    out = _output(engine)
    assert out.payload["is_error"] is True
    assert "may be called again safely" in out.payload["output"]
    assert calls == []  # 恢复不执行工具
    assert engine._find_active_suspension() is None  # noqa: SLF001
    await pool.close()


async def test_non_idempotent_intent_suspends_for_operator(
    skills_dir: Path, threads_dir: Path
) -> None:
    """非幂等工具：挂起交人裁决，新用户消息被活跃挂起守卫拒绝。"""
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "transfer", session_id="s0")
    await setup.close()

    pool = await _resume_pool(
        skills_dir, threads_dir, [_tool("transfer", effect_kind="external_non_idempotent",
                                        reconciliation="manual")])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)

    record = engine._find_active_suspension()  # noqa: SLF001
    assert record is not None
    (pending,) = record.pending
    assert pending.reason is SuspendReason.TOOL_OUTCOME_UNKNOWN
    assert pending.related_call_id == "call-1"
    assert pending.detail["tool"] == "transfer"
    assert pending.detail["effect_kind"] == "external_non_idempotent"
    assert "function_call_output" not in _kinds(engine)
    await pool.close()


async def test_operator_provide_fills_output_and_continues(
    skills_dir: Path, threads_dir: Path
) -> None:
    """人给出真实结局 → 原样回填（保留 is_error），turn 续跑到完成。"""
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "transfer", session_id="s0")
    await setup.close()

    pool = await _resume_pool(
        skills_dir, threads_dir,
        [_tool("transfer", effect_kind="external_non_idempotent", reconciliation="manual")],
        turns=[SimTurn(text="转账已确认完成")])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)
    record = engine._find_active_suspension()  # noqa: SLF001
    req_id = record.pending[0].request_id

    sub_id = await engine.submit(Resume(thread_id=tid, resolutions={
        req_id: {"action": "provide", "output": "已到账，流水号 42", "is_error": False}}))
    done = []
    async for ev in engine.subscribe(sub_id):
        done.append(ev.msg.kind)
        if ev.msg.kind in ("turn_completed", "turn_failed", "suspension_resolve_rejected"):
            break
    assert "turn_completed" in done, done
    out = _output(engine)
    assert out.payload["output"] == "已到账，流水号 42"
    assert out.payload["is_error"] is False
    await pool.close()


async def test_operator_retry_reexecutes_tool(
    skills_dir: Path, threads_dir: Path
) -> None:
    """人裁决 retry → 内核重新执行该调用并回填真实结果。"""
    calls: list[str] = []
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "transfer", session_id="s0")
    await setup.close()

    pool = await _resume_pool(
        skills_dir, threads_dir,
        [_tool("transfer", effect_kind="external_non_idempotent",
               reconciliation="manual", calls=calls)],
        turns=[SimTurn(text="完成")])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)
    req_id = engine._find_active_suspension().pending[0].request_id  # noqa: SLF001

    sub_id = await engine.submit(Resume(thread_id=tid, resolutions={req_id: {"action": "retry"}}))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed", "suspension_resolve_rejected"):
            break
    assert calls == ["transfer"]
    assert _output(engine).payload["output"] == "transfer executed"
    await pool.close()


@pytest.mark.parametrize(
    ("verdict", "expect_suspended", "expect_text"),
    [
        (ReconcileVerdict(status="completed", output="订单 7 已创建"), False, "订单 7 已创建"),
        (ReconcileVerdict(status="not_executed"), False, "not executed"),
        (ReconcileVerdict(status="unknown"), True, None),
    ],
)
async def test_reconcile_verdict_routes_recovery(
    skills_dir: Path, threads_dir: Path,
    verdict: ReconcileVerdict, expect_suspended: bool, expect_text: str | None,
) -> None:
    """提供回查函数：completed 回填真实结果；not_executed 可安全重发；unknown 交人。"""
    seen: list[tuple[dict, str]] = []

    async def _reconcile(arguments: dict, call_id: str) -> ReconcileVerdict:
        seen.append((arguments, call_id))
        return verdict

    tool = _tool("create_order", effect_kind="reconcilable",
                 reconciliation="query", reconcile=_reconcile)
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "create_order", session_id="s0")
    await setup.close()

    pool = await _resume_pool(skills_dir, threads_dir, [tool])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)

    assert seen == [({"id": "A-17"}, "call-1")]
    assert (engine._find_active_suspension() is not None) is expect_suspended  # noqa: SLF001
    if expect_text is not None:
        assert expect_text in _output(engine).payload["output"]
    await pool.close()


async def test_reconcile_exception_falls_back_to_operator(
    skills_dir: Path, threads_dir: Path
) -> None:
    """回查函数抛异常 → 视同查不清，挂起交人（不阻断恢复）。"""

    async def _boom(arguments: dict, call_id: str) -> ReconcileVerdict:
        raise RuntimeError("downstream unavailable")

    tool = _tool("create_order", effect_kind="reconcilable",
                 reconciliation="query", reconcile=_boom)
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "create_order", session_id="s0")
    await setup.close()

    pool = await _resume_pool(skills_dir, threads_dir, [tool])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)
    assert engine._find_active_suspension() is not None  # noqa: SLF001
    await pool.close()


async def test_recovery_is_idempotent_across_restarts(
    skills_dir: Path, threads_dir: Path
) -> None:
    """再次冷恢复不重复补写 function_call / output / 挂起。"""
    tools = [_tool("transfer", effect_kind="external_non_idempotent", reconciliation="manual")]
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "transfer", session_id="s0")
    await setup.close()

    for session in ("s1", "s2"):
        pool = await _resume_pool(skills_dir, threads_dir, tools)
        engine = await pool.get_or_create(
            session_id=session, entry_skill_id="code-reviewer", resume_thread_id=tid)
        kinds = _kinds(engine)
        await pool.close()
    assert kinds.count("function_call") == 1
    assert kinds.count("suspension") == 1


async def test_intent_is_durable_before_tool_executes(
    skills_dir: Path, threads_dir: Path
) -> None:
    """热路径：工具 handler 运行时，store 里已经有它的 write-ahead 意图。"""
    observed: list[list[str]] = []
    holder: dict[str, Any] = {}

    async def _handler(args: dict, ctx: Any) -> ToolResult:
        gen = await holder["pool"].store.load_thread(ctx.thread_id)
        observed.append([it.kind for it in [i async for i in gen]])
        return ToolResult.ok("ok")

    tool = ToolSpec(name="probe", description="probe",
                    input_schema={"type": "object", "properties": {}}, handler=_handler)
    client = SimClient(turns=[
        SimTurn(text="", tool_calls=[{"id": "c9", "name": "probe", "arguments": "{}"}]),
        SimTurn(text="done"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[tool])
    holder["pool"] = pool
    engine = await pool.get_or_create(session_id="hot", entry_skill_id="code-reviewer")
    # code-reviewer 的白名单不含 probe：直接放行本工具以驱动真实派发路径
    entry = engine._entry_skill  # noqa: SLF001
    engine._entry_skill = replace(  # noqa: SLF001
        entry, tool_names=frozenset({*entry.tool_names, "probe"}))

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await wait_for_condition(lambda: bool(observed), deadline_seconds=GUARD_TIMEOUT_SECONDS)
    assert "tool_intent" in observed[0]
    assert "function_call_output" not in observed[0]
    await pool.close()


def test_find_dangling_calls_skips_completed_and_suspended() -> None:
    """已有 output 的调用、活跃挂起持有的调用都不算悬空。"""
    from taifeng.conversation.models import function_call, function_call_output

    tid = "t"
    record = SuspensionRecord(
        record_id="r1", thread_id=tid, submission_id="s", turn_index=0,
        pending=(PendingRequest(request_id="q", reason=SuspendReason.DATA,
                                related_call_id="held"),),
        created_at=0)
    history = (
        tool_intent_item("done", "x", "{}", thread_id=tid),
        function_call("done", "x", "{}", thread_id=tid),
        function_call_output("done", "ok", thread_id=tid),
        tool_intent_item("held", "x", "{}", thread_id=tid),
        function_call("held", "x", "{}", thread_id=tid),
        record.to_item(),
        tool_intent_item("lost", "x", "{}", thread_id=tid),
    )
    dangling = find_dangling_calls(history)
    assert [d.call_id for d in dangling] == ["lost"]
    assert dangling[0].has_function_call is False


def _unknown_record(call_id: str = "c") -> SuspensionRecord:
    return SuspensionRecord(
        record_id="r", thread_id="t", submission_id="s", turn_index=0,
        pending=(PendingRequest(request_id="q", reason=SuspendReason.TOOL_OUTCOME_UNKNOWN,
                                related_call_id=call_id),),
        created_at=0)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"action": "provide"}, "tool_outcome_provide_requires_output"),
        ({"action": "rerun"}, "invalid_tool_outcome_action"),
        ("retry", "invalid_payload_shape"),
    ],
)
def test_resolver_rejects_invalid_tool_outcome_payload(payload: Any, message: str) -> None:
    """非法裁决显式拒绝（禁静默兜底）。"""
    with pytest.raises(ResolveError, match=message):
        SuspensionResolver().plan(_unknown_record(), {"q": payload})


def test_resolver_plans_each_tool_outcome_action() -> None:
    """retry → 重新执行；provide → 原样回填；abort → 中止说明 + 终止续跑。"""
    resolver = SuspensionResolver()
    assert resolver.plan(_unknown_record(), {"q": {"action": "retry"}}).execute_tool_call_ids == ["c"]
    provided = resolver.plan(
        _unknown_record(), {"q": {"action": "provide", "output": "x", "is_error": True}})
    assert provided.provided_outputs == {"c": ("x", True)}
    aborted = resolver.plan(_unknown_record(), {"q": {"action": "abort"}})
    assert aborted.abort and "c" in aborted.deny_outputs


def test_tool_outcome_unknown_rejects_retry_on_expire() -> None:
    """交人裁决类到期只能 abort（无法替人判断副作用是否发生）。"""
    with pytest.raises(ValueError, match="on_expire"):
        PendingRequest(request_id="q", reason=SuspendReason.TOOL_OUTCOME_UNKNOWN,
                       related_call_id="c", ttl_seconds=10, on_expire="retry")


async def test_new_user_message_refused_while_awaiting_operator(
    skills_dir: Path, threads_dir: Path
) -> None:
    """交人裁决期间提交新用户消息 → 被活跃挂起守卫拒绝（不会越过未知结局继续跑）。"""
    setup = await _resume_pool(skills_dir, threads_dir, [])
    tid = await _crashed_thread(setup, "transfer", session_id="s0")
    await setup.close()

    pool = await _resume_pool(
        skills_dir, threads_dir,
        [_tool("transfer", effect_kind="external_non_idempotent", reconciliation="manual")])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer", resume_thread_id=tid)
    sub_id = await engine.submit(taifeng.UserMessage(text="继续"))
    kinds: list[str] = []

    async def _collect() -> None:
        async for ev in engine.subscribe(sub_id):
            kinds.append(ev.msg.kind)
            if ev.msg.kind in ("turn_refused", "turn_failed", "turn_completed"):
                return

    await asyncio.wait_for(_collect(), timeout=GUARD_TIMEOUT_SECONDS)
    assert "turn_completed" not in kinds, kinds
    await pool.close()
