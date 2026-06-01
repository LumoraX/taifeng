"""通用挂起 / resume 原语测试。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from taifeng.conversation.models import user_message
from taifeng.llm.errors import LLMError
from taifeng.permission.types import PermissionRequest, SuspendingPrompter
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.signal import SuspendSignal


def test_suspend_reason_values():
    # 四类挂起原因,值用于 JSON 序列化稳定性
    assert SuspendReason.PERMISSION.value == "permission"
    assert SuspendReason.FORM.value == "form"
    assert SuspendReason.DATA.value == "data"
    assert SuspendReason.SYSTEM_RETRY.value == "system_retry"


def test_pending_request_frozen_and_fields():
    req = PendingRequest(
        request_id="req_1",
        reason=SuspendReason.PERMISSION,
        payload_schema={"type": "object"},
        related_call_id="call_abc",
        detail={"scope": "tool_use", "target": "shell_exec"},
    )
    assert req.request_id == "req_1"
    assert req.reason is SuspendReason.PERMISSION
    assert req.related_call_id == "call_abc"
    # frozen:不可变
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.request_id = "x"  # type: ignore[misc]


def test_pending_request_default_dicts_are_independent():
    # 默认 payload_schema / detail 必须是各实例独立的对象(field default_factory),
    # 不能共享同一 dict,否则一处 mutate 会污染其他实例
    a = PendingRequest(request_id="a", reason=SuspendReason.FORM)
    b = PendingRequest(request_id="b", reason=SuspendReason.FORM)
    assert a.payload_schema is not b.payload_schema
    assert a.detail is not b.detail


def test_suspend_reason_json_serializes_to_string():
    # StrEnum 跨层序列化契约:json.dumps 直接得到字符串值(若有人改回普通 Enum 会回归)
    assert json.dumps({"r": SuspendReason.FORM}) == '{"r": "form"}'
    assert str(SuspendReason.PERMISSION) == "permission"


def test_suspend_signal_carries_pending():
    """SuspendSignal 携带 PendingRequest,且是 Exception 子类但非 LLMError 子类。"""
    req = PendingRequest(request_id="r1", reason=SuspendReason.FORM)
    sig = SuspendSignal(req)
    assert sig.pending is req
    # 是 Exception 子类(控制流),但不是 LLMError 家族
    assert isinstance(sig, Exception)
    assert not isinstance(sig, LLMError)


def test_suspension_item_constructor():
    from taifeng.conversation.models import suspension_item

    item = suspension_item(
        record_id="sr_1",
        submission_id="sub_1",
        turn_index=2,
        pending=[{"request_id": "r1", "reason": "permission", "payload_schema": {},
                  "related_call_id": "call_a", "detail": {}}],
        created_at=1000,
        thread_id="th_1",
    )
    assert item.kind == "suspension"
    assert item.thread_id == "th_1"
    assert item.payload["record_id"] == "sr_1"
    assert item.payload["turn_index"] == 2
    assert item.payload["pending"][0]["request_id"] == "r1"
    assert item.payload["resolved"] is False
    assert item.payload["created_at"] == 1000


def test_record_roundtrip_via_item():
    """SuspensionRecord → to_item() → from_item() 必须完整还原,含 SuspendReason 枚举类型。"""
    rec = SuspensionRecord(
        record_id="sr_1",
        thread_id="th_1",
        submission_id="sub_1",
        turn_index=1,
        pending=(
            PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION,
                           related_call_id="call_a", detail={"scope": "tool_use"}),
            PendingRequest(request_id="r2", reason=SuspendReason.FORM,
                           related_call_id="call_b"),
        ),
        created_at=1234,
    )
    item = rec.to_item()
    assert item.kind == "suspension"
    back = SuspensionRecord.from_item(item)
    assert back == rec
    # 枚举还原正确(不是裸字符串)
    assert back.pending[0].reason is SuspendReason.PERMISSION
    assert back.pending[1].related_call_id == "call_b"


def test_record_request_ids():
    """request_ids() 返回全部 pending 的 request_id 集合。"""
    rec = SuspensionRecord(
        record_id="sr", thread_id="t", submission_id="s", turn_index=1,
        pending=(PendingRequest(request_id="a", reason=SuspendReason.DATA),
                 PendingRequest(request_id="b", reason=SuspendReason.DATA)),
        created_at=1,
    )
    assert rec.request_ids() == {"a", "b"}


def test_record_from_item_rejects_wrong_kind():
    """from_item 传入非 suspension item 必须抛 ValueError。"""
    bad = user_message("hi", thread_id="t")
    with pytest.raises(ValueError):
        SuspensionRecord.from_item(bad)


async def test_suspending_prompter_raises_signal():
    """ask 模式不阻塞,而是抛 SuspendSignal(reason=PERMISSION)。"""
    prompter = SuspendingPrompter()
    req = PermissionRequest.for_tool_call(
        "shell_exec", {"cmd": "rm -rf /tmp/x"},
        thread_id="th", submission_id="sub", entry_skill_id="root",
        turn_index=1, call_chain=("root",),
    )
    with pytest.raises(SuspendSignal) as ei:
        await prompter.prompt(req)
    pending = ei.value.pending
    assert pending.reason is SuspendReason.PERMISSION
    assert pending.detail["scope"] == "tool_use"
    assert pending.detail["target"] == "shell_exec"
    assert pending.request_id  # 非空


def test_outcome_has_optional_suspend_field():
    # ToolCallOutcome 新增 suspend 字段,默认 None(正常完成的 outcome)
    from taifeng.loop.tool_batch import ToolCallOutcome

    fields = {f.name for f in dataclasses.fields(ToolCallOutcome)}
    assert "suspend" in fields


# ====================================================================
# 防御纵深:SuspendSignal 必须穿透上游宽 except(否则 HITL 暂停被静默吞)
# ====================================================================


async def test_permission_check_propagates_suspend_signal():
    """SuspendingPrompter 经 PermissionPolicy.check 必须抛 SuspendSignal,
    而不是被宽 except 吞成 deny(否则 HITL 暂停被静默转成拒绝)。"""
    from taifeng.permission.types import PermissionPolicy

    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    req = PermissionRequest.for_tool_call(
        "shell_exec",
        {"cmd": "echo hi"},
        thread_id="t",
        submission_id="s",
        entry_skill_id="root",
        turn_index=1,
        call_chain=("root",),
    )
    with pytest.raises(SuspendSignal):
        await policy.check(req)


async def test_runtime_invoke_propagates_suspend_signal():
    """工具 handler 抛 SuspendSignal 时 ToolCallRuntime 必须放行,不吞成 ToolResult.error。

    构造一个 handler 直接抛 SuspendSignal 的 ToolSpec,注册进 runtime,经
    dispatch 应原样向上抛出而非吞成 reason=exception 的 ToolResult。
    """
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime
    from taifeng.tool.spec import ToolContext, ToolSpec

    # 触发挂起的 handler:模拟权限 ask 深处抛 SuspendSignal
    async def _suspending_handler(args, ctx):
        pending = PendingRequest(
            request_id="req_x",
            reason=SuspendReason.PERMISSION,
            related_call_id=ctx.call_id,
        )
        raise SuspendSignal(pending)

    spec = ToolSpec(
        name="needs_approval",
        description="抛挂起信号的测试工具",
        input_schema={"type": "object"},
        handler=_suspending_handler,
    )
    runtime = ToolCallRuntime(ToolRegistry([spec]))
    ctx = ToolContext(call_id="call_1", cancel=CancellationToken(), thread_id="t")

    with pytest.raises(SuspendSignal):
        await runtime.dispatch(name="needs_approval", arguments={}, ctx=ctx)


# ====================================================================
# Task 7 集成:run_turn 命中挂起点 → 退栈为 suspended 结局 + 落 suspension 断点
# ====================================================================
#
# 走 EnginePool → AgentEngine → 一次 turn 的真实链路(参照 tests/loop/test_engine_e2e.py
# 与 tests/loop/test_permission_policy_wiring.py 的 MockClient + EnginePool 搭建套路):
#   - 注册一个工具,其 handler 主动经注入的 PermissionPolicy.check 走审批门控;
#   - PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()) → check 抛
#     SuspendSignal(reason=PERMISSION);
#   - runtime._invoke 放行 SuspendSignal → dispatch_batch 捕获为 outcome.suspend;
#   - 阶段 3 配对回填区识别 suspend → 只落 function_call(无 output)、抛 _BatchSuspend;
#   - run() 捕获 _BatchSuspend → end_reason="suspended" + 落 SuspensionRecord。
# 选用集成测试(而非直接单测 _dispatch_tools)因为它同时验证了 end_reason 这一关键结局。


async def _gated_tool_factory():
    """构造一个工具:handler 经 ctx.extras 注入的 PermissionPolicy 走审批门控。

    门控为 ask 模式时 check() 抛 SuspendSignal,据此触发 turn 挂起;
    返回值仅在门控放行(此场景不会发生)时使用。
    """
    from taifeng.permission.types import PermissionRequest
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    async def handler(args, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        # 构造审批请求并走门控:ask 模式下 SuspendingPrompter 抛 SuspendSignal,
        # 由 runtime._invoke 放行 → dispatch_batch 捕获为 outcome.suspend。
        req = PermissionRequest.for_tool_call(
            "danger",
            args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("root",),
        )
        await policy.check(req)
        return ToolResult.ok("approved")  # 门控放行才到这(本场景不会)

    return ToolSpec(
        name="danger",
        description="需审批的危险工具(测试用)",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


async def test_suspend_turn_ends_suspended_and_persists_record(skills_dir, threads_dir):
    """run_turn 命中挂起点时:end_reason=="suspended"、落 SuspensionRecord、
    function_call 有但无配对 function_call_output(history-gap)。"""
    import taifeng
    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.permission.types import PermissionPolicy

    gated_tool = await _gated_tool_factory()

    # entry skill 声明 tool_names 含 danger,否则会被 turn.py 的白名单过滤掉
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

    # MockClient:第一轮产出一个 danger tool call(命中审批挂起);无第二轮(turn 挂起不再采样)
    client = MockClient(turns=[
        MockTurn(
            text="calling danger",
            tool_calls=[{"id": "call_d1", "name": "danger", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
    ])

    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[gated_tool],
        permission_policy=policy,
    )
    engine = await pool.get_or_create(
        session_id="suspend-e2e", entry_skill_id="suspend-skill",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    # 合约断言:挂起 turn 必须以 turn_completed 结束,而非 turn_failed
    assert ev.msg.kind == "turn_completed", f"挂起 turn 不应失败,实得 {ev.msg.kind}"
    assert ev.msg.data.get("end_reason") == "suspended", (
        f"挂起 turn 的 end_reason 应为 'suspended',实得 {ev.msg.data.get('end_reason')!r}"
    )

    # 落盘验证:store 含 kind=="suspension" item;call_d1 有 function_call 无 output
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "应恰好落一条 suspension item"

    # 还原为 SuspensionRecord 校验 pending
    rec = SuspensionRecord.from_item(suspension_items[0])
    assert len(rec.pending) == 1
    assert rec.pending[0].reason is SuspendReason.PERMISSION

    # history-gap:danger 的 function_call 已落,但无配对的 function_call_output
    fc_ids = {
        it.payload.get("call_id")
        for it in items
        if it.kind == "function_call"
    }
    fco_ids = {
        it.payload.get("call_id")
        for it in items
        if it.kind == "function_call_output"
    }
    assert "call_d1" in fc_ids, "挂起的 function_call 必须落盘"
    assert "call_d1" not in fco_ids, "挂起点不得有 function_call_output(history-gap)"


# ============================================================
# Task 8 A：system_retry —— 可恢复 LLMError 转 SYSTEM_RETRY 挂起
# ============================================================


def test_should_suspend_classifies_recoverable():
    """_should_suspend_on_error:可恢复 / 等外部介入 → True;确定性失败 → False。"""
    from taifeng.llm.errors import (
        AuthenticationError,
        ContentFilterError,
        ContextOverflowError,
        InvalidRequestError,
        RateLimitError,
    )
    from taifeng.loop.turn import _should_suspend_on_error

    # 可恢复(retryable=True)/ 等外部条件(provider_auth) → 挂起
    assert _should_suspend_on_error(RateLimitError("rl")) is True
    assert _should_suspend_on_error(AuthenticationError("bad key")) is True
    # 确定性失败(retryable=False 且 failure_class 不在等外部介入类) → 不挂起,硬失败
    assert _should_suspend_on_error(ContentFilterError("blocked")) is False
    assert _should_suspend_on_error(ContextOverflowError("too long")) is False
    assert _should_suspend_on_error(InvalidRequestError("bad req")) is False
    # 非 LLMError → 不挂起
    assert _should_suspend_on_error(ValueError("x")) is False


async def test_system_retry_suspends_turn(skills_dir, threads_dir):
    """retry 耗尽后 stream 抛 RateLimitError(可恢复)→ turn 挂起为 SYSTEM_RETRY。

    用一个 stream 直接抛 RateLimitError 的最小 client(模拟 retry 已耗尽);
    断言 turn 以 end_reason=="suspended" 结束,落一条 SuspensionRecord,
    其单个 pending.reason == SYSTEM_RETRY、related_call_id is None、
    detail["failure_class"] 已填。
    """
    import taifeng
    from taifeng.llm.client import ModelClient
    from taifeng.llm.errors import RateLimitError

    class _RaisingSession:
        """stream 直接抛 RateLimitError(模拟 provider retry 已耗尽)。"""

        def __init__(self, cancel):  # noqa: ANN001, ANN204
            self._cancel = cancel

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            pass

        async def stream(self, request):  # noqa: ANN001, ANN201
            # 必须先 yield 才是 async generator;yield 前抛即在首次迭代抛出
            raise RateLimitError("rate limited", retry_after_seconds=3.0)
            yield  # pragma: no cover —— 使函数成为 async generator

    class _RaisingClient(ModelClient):
        def session(self, *, cancel, model=None):  # noqa: ANN001, ANN201
            return _RaisingSession(cancel)

    skill_md = """---
name: retry-skill
description: system_retry entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
max_call_depth: 2
---
# Retry
"""
    (skills_dir / "retry-skill").mkdir()
    (skills_dir / "retry-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=_RaisingClient(),
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="retry-e2e", entry_skill_id="retry-skill",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    # 可恢复错误转挂起 → turn 以 suspended 正常结束,而非 turn_failed
    assert ev.msg.kind == "turn_completed", f"system_retry 应挂起非失败,实得 {ev.msg.kind}"
    assert ev.msg.data.get("end_reason") == "suspended"

    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "应恰好落一条 suspension item"
    rec = SuspensionRecord.from_item(suspension_items[0])
    assert len(rec.pending) == 1
    pending = rec.pending[0]
    assert pending.reason is SuspendReason.SYSTEM_RETRY
    assert pending.related_call_id is None
    # detail 携带可恢复错误的归因信息(R1:taifeng 仅携带不解析)
    assert pending.detail["failure_class"] == "provider_rate_limit"
    assert pending.detail["kind"] == "RateLimitError"


# ============================================================
# Task 8 B：builtins 透传 call_id 到 permission 的 PendingRequest.related_call_id
# ============================================================


async def test_permission_pending_carries_call_id():
    """SuspendingPrompter 经 builtin(shell)的 PermissionRequest 挂起时,
    PendingRequest.related_call_id 等于该 tool call 的 call_id。

    验证 builtin 在构造 PermissionRequest 时把 ctx.call_id 写入 metadata["call_id"],
    使 SuspendingPrompter 能把它填进 PendingRequest.related_call_id。
    """
    from taifeng.permission.types import PermissionPolicy
    from taifeng.tool.builtins.shell import make_shell_exec_tool
    from taifeng.tool.spec import ToolContext

    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    tool = make_shell_exec_tool(policy=policy)
    ctx = ToolContext(
        call_id="call_shell_1",
        thread_id="th",
        cancel=_noop_cancel(),
        extras={"submission_id": "sub", "entry_skill_id": "root", "turn_index": 1},
    )
    with pytest.raises(SuspendSignal) as ei:
        await tool.handler({"command": "echo hi"}, ctx)
    # related_call_id 必须等于发起该工具调用的 call_id(history-gap 配对依据)
    assert ei.value.pending.related_call_id == "call_shell_1"


def _noop_cancel():
    """构造一个未取消的 CancellationToken(测试用)。"""
    from taifeng.loop.cancellation import CancellationToken

    return CancellationToken()


def _make_tool_ctx(call_id: str):
    """构造最小 ToolContext(测试用)。"""
    from taifeng.tool.spec import ToolContext

    return ToolContext(call_id=call_id, cancel=_noop_cancel(), thread_id="t")


async def test_request_user_input_raises_data_suspend():
    """request_user_input 被调用 → 抛 SuspendSignal(reason=DATA),
    related_call_id == 本次 call_id,prompt/response_schema 进 detail(不透明透传)。"""
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    spec = make_request_user_input_tool()
    assert spec.name == "request_user_input"
    assert spec.parallel_safe is False
    ctx = _make_tool_ctx(call_id="call_xyz")
    with pytest.raises(SuspendSignal) as ei:
        await spec.handler(
            {"prompt": "你的年龄?", "response_schema": {"type": "integer"}}, ctx,
        )
    p = ei.value.pending
    assert p.reason is SuspendReason.DATA
    assert p.related_call_id == "call_xyz"
    assert p.request_id == "call_xyz"
    assert p.detail["prompt"] == "你的年龄?"
    assert p.detail["response_schema"] == {"type": "integer"}
    # payload_schema 直接透传 response_schema(R1:内核不解析)
    assert p.payload_schema == {"type": "integer"}


async def test_request_user_input_empty_prompt_rejected():
    """空 prompt → typed error(禁静默占位)。"""
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    spec = make_request_user_input_tool()
    ctx = _make_tool_ctx(call_id="c1")
    with pytest.raises(ValueError):
        await spec.handler({"prompt": "", "response_schema": {}}, ctx)


# ============================================================
# Task 9：三个新 EventMsg 变体
# ============================================================


def test_new_event_msgs():
    """TurnSuspended / SuspensionResolved / SuspensionResolveRejected 三个新 EventMsg。

    按 EventMsg 实际 API 构造（外层 EventMsg 包裹内层 _Msg 子类），验证：
    - kind 值正确；
    - 可通过 discriminated-union 反序列化（round-trip）；
    - 三个类均已加入 EventMsg Union（pydantic 判别器可识别）。
    """
    from taifeng.loop.event import (
        EventMsg,
        SuspensionResolved,
        SuspensionResolveRejected,
        TurnSuspended,
    )

    # TurnSuspended
    e1 = EventMsg(
        submission_id="s",
        msg=TurnSuspended(data={
            "thread_id": "t",
            "record_id": "sr",
            "pending": [],
            "cache_invalidated": True,
        }),
    )
    assert e1.msg.kind == "turn_suspended"
    # round-trip 验证 discriminated-union 配线正确
    e1b = EventMsg.model_validate_json(e1.model_dump_json())
    assert e1b.msg.kind == "turn_suspended"
    assert e1b.msg.data["record_id"] == "sr"

    # SuspensionResolved
    e2 = EventMsg(
        submission_id="s",
        msg=SuspensionResolved(data={"record_id": "sr", "request_ids": ["r1"]}),
    )
    assert e2.msg.kind == "suspension_resolved"
    e2b = EventMsg.model_validate_json(e2.model_dump_json())
    assert e2b.msg.kind == "suspension_resolved"
    assert e2b.msg.data["request_ids"] == ["r1"]

    # SuspensionResolveRejected
    e3 = EventMsg(
        submission_id="s",
        msg=SuspensionResolveRejected(data={"reason": "unknown_request_id"}),
    )
    assert e3.msg.kind == "suspension_resolve_rejected"
    e3b = EventMsg.model_validate_json(e3.model_dump_json())
    assert e3b.msg.kind == "suspension_resolve_rejected"
    assert e3b.msg.data["reason"] == "unknown_request_id"


# ============================================================
# Task 10：Resume Op —— 业务侧提交续跑意图
# ============================================================


def test_resume_op_in_union():
    from taifeng.loop.submission import Resume, Submission

    op = Resume(thread_id="th_1", resolutions={"r1": {"granted": True}})
    assert op.kind == "resume"
    sub = Submission(op=op)
    assert sub.op.thread_id == "th_1"
    assert sub.op.resolutions["r1"]["granted"] is True
    # 经判别式 union 解析往返(确认 kind=resume 正确路由)
    sub2 = Submission.model_validate_json(sub.model_dump_json())
    assert sub2.op.kind == "resume"
    assert sub2.op.resolutions["r1"]["granted"] is True


# ============================================================
# Task 11：SuspensionResolver —— 把 resolutions 配回 record.pending
# ============================================================


def _rec(*reqs):
    from taifeng.suspend.record import SuspensionRecord
    return SuspensionRecord(record_id="sr", thread_id="t", submission_id="s",
                            turn_index=1, pending=tuple(reqs), created_at=1)


def test_resolver_rejects_incomplete():
    import pytest

    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import ResolveError, SuspensionResolver
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.FORM),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM),
    )
    with pytest.raises(ResolveError):
        SuspensionResolver().validate(rec, {"r1": {"x": 1}})   # 缺 r2


def test_resolver_rejects_unknown():
    import pytest

    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import ResolveError, SuspensionResolver
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.FORM))
    with pytest.raises(ResolveError):
        SuspensionResolver().validate(rec, {"r1": {}, "rX": {}})   # 多余 rX


def test_resolver_classifies_outputs():
    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import SuspensionResolver
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION, related_call_id="ca"),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM, related_call_id="cb"),
        PendingRequest(request_id="r3", reason=SuspendReason.SYSTEM_RETRY),
    )
    plan = SuspensionResolver().plan(rec, {
        "r1": {"granted": True},
        "r2": {"answer": "hello"},
        "r3": {"action": "retry"},
    })
    assert plan.execute_tool_call_ids == ["ca"]
    assert plan.direct_outputs["cb"] == {"answer": "hello"}
    assert plan.resample is True


def test_resolver_permission_deny_path():
    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import SuspensionResolver
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.PERMISSION, related_call_id="ca")
    )
    plan = SuspensionResolver().plan(rec, {"r1": {"granted": False, "reason": "no"}})
    assert plan.execute_tool_call_ids == []
    assert plan.deny_outputs["ca"] == "no"


def test_resolver_system_retry_abort():
    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import SuspensionResolver
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY))
    plan = SuspensionResolver().plan(rec, {"r1": {"action": "abort"}})
    assert plan.abort is True
    assert plan.resample is False
