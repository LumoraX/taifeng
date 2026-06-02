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
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break

    # 合约断言:挂起 turn 以独立终结态 turn_suspended 结束(R3),而非 turn_completed / turn_failed
    assert ev.msg.kind == "turn_suspended", f"挂起 turn 应发 turn_suspended,实得 {ev.msg.kind}"

    # 落盘验证:store 含 kind=="suspension" item;call_d1 有 function_call 无 output
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1, "应恰好落一条 suspension item"

    # 还原为 SuspensionRecord 校验 pending
    rec = SuspensionRecord.from_item(suspension_items[0])
    assert len(rec.pending) == 1
    assert rec.pending[0].reason is SuspendReason.PERMISSION

    # turn_suspended 事件 data 必须携带与持久化 record 一致的 record_id 与 pending
    assert ev.msg.data["record_id"] == rec.record_id, "事件 record_id 应匹配落盘 record"
    assert ev.msg.data["thread_id"] == engine.thread_id
    assert ev.msg.data["cache_invalidated"] is True
    ev_pending = ev.msg.data["pending"]
    assert len(ev_pending) == 1, "事件 pending 长度应与 record 一致"
    assert ev_pending[0]["reason"] == SuspendReason.PERMISSION.value

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
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break

    # 可恢复错误转挂起 → turn 发独立终结态 turn_suspended(R3),而非 turn_failed
    assert ev.msg.kind == "turn_suspended", f"system_retry 应挂起非失败,实得 {ev.msg.kind}"
    assert ev.msg.data["thread_id"] == engine.thread_id
    assert ev.msg.data["cache_invalidated"] is True
    suspended_data = ev.msg.data

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

    # turn_suspended 事件的 record_id / pending 必须与落盘 record 对齐(R3)
    assert suspended_data["record_id"] == rec.record_id
    assert len(suspended_data["pending"]) == 1
    assert suspended_data["pending"][0]["reason"] == SuspendReason.SYSTEM_RETRY.value


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


def test_resolver_permission_granted_missing_call_id_raises():
    import pytest

    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import ResolveError, SuspensionResolver
    rec = _rec(PendingRequest(
        request_id="r1", reason=SuspendReason.PERMISSION, related_call_id=None,
    ))
    with pytest.raises(ResolveError):
        SuspensionResolver().plan(rec, {"r1": {"granted": True}})


def test_resolver_form_missing_call_id_raises():
    import pytest

    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.resolver import ResolveError, SuspensionResolver
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.FORM, related_call_id=None))
    with pytest.raises(ResolveError):
        SuspensionResolver().plan(rec, {"r1": {"answer": "x"}})


# ============================================================
# Task 12b：engine _handle_resume —— 配对回填 + 执行被批准 tool + 续跑挂起 turn
# ============================================================


async def _drain_until_terminal(engine, sub_id):
    """订阅指定 submission 直到终结事件，返回收到的事件列表。

    终结事件含三类：turn_completed（正常完成）/ turn_failed（失败）/
    turn_suspended（挂起独立终结态，R3 对齐）。挂起 turn 不再发 turn_completed，
    因此 break 条件必须纳入 turn_suspended，否则挂起阶段会卡死在订阅循环。
    """
    events = []
    async for ev in engine.subscribe(sub_id):
        events.append(ev)
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break
    return events


async def _gated_tool_with_call_id_factory():
    """构造一个需审批的工具：门控时把 ctx.call_id 透传进 PermissionRequest.metadata。

    与 `_gated_tool_factory` 的区别：本工具显式 ``extra_metadata={"call_id": ...}``，
    使 SuspendingPrompter 能把 call_id 填进 PendingRequest.related_call_id —— 这是
    permission-allow resume 路径定位「该执行哪个挂起 tool」的依据。门控放行时返回真实结果。
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
            call_chain=("root",),
            extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        # resume 二次放行时（policy 已不再 ask）走到这里，返回真实结果用于回填 output
        return ToolResult.ok("danger executed")

    return ToolSpec(
        name="danger",
        description="需审批的危险工具（透传 call_id，测试用）",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


def _build_suspend_skill(skills_dir):
    """写入一个声明 danger 工具的 entry composite skill（与挂起用例对齐）。"""
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


async def test_resume_after_permission_suspension(skills_dir, threads_dir):
    """permission ask 挂起 → Resume(granted=True) → 执行被批准 tool + 续采样完成。

    链路：
      1. MockClient 第一轮产出 danger tool call → 经 ask 门控挂起为 PERMISSION。
      2. 提交 Resume(resolutions={req_id: {"granted": True}})。
      3. 断言：suspension_resolved 事件触发；被挂起的 call 现在有了
         function_call_output（gap 补齐）；续采样的第二轮以 turn_completed 结束。
    """
    import taifeng
    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.submission import Resume
    from taifeng.permission.types import (
        PermissionPolicy,
        SuspendingPrompter,
    )

    # 用真实的 SuspendingPrompter（总是挂起）：resume 续跑必须靠 engine 的一次性预批准
    # 放行，而不再触发 prompter（Task 12c：否则会无限挂起死循环）。
    gated_tool = await _gated_tool_with_call_id_factory()
    _build_suspend_skill(skills_dir)

    # 第一轮：danger tool call（命中审批挂起）；第二轮（resume 后续跑）：纯文本完成
    client = MockClient(turns=[
        MockTurn(
            text="calling danger",
            tool_calls=[{"id": "call_d1", "name": "danger", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
        MockTurn(
            text="approved and done",
            usage=TokenUsage(input_tokens=8, output_tokens=4),
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
        session_id="resume-perm-e2e", entry_skill_id="suspend-skill",
    )

    # === 第一轮：触发挂起 ===
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await _drain_until_terminal(engine, sub_id)
    # 挂起阶段以独立终结态 turn_suspended 收尾（R3）
    assert events1[-1].msg.kind == "turn_suspended"

    # 从持久化的 suspension record 取 request_id（续跑入参）
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1
    rec = SuspensionRecord.from_item(suspension_items[0])
    req_id = rec.pending[0].request_id
    assert rec.pending[0].related_call_id == "call_d1"

    # === 第二轮：Resume 续跑 ===
    resume_sub = await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"granted": True}},
    ))
    events2 = await _drain_until_terminal(engine, resume_sub)

    # 续跑落盘后、close 前读取最新 items（验证 gap 补齐）
    items2 = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    # suspension_resolved 必须触发
    kinds2 = [ev.msg.kind for ev in events2]
    assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
    resolved_ev = next(ev for ev in events2 if ev.msg.kind == "suspension_resolved")
    assert resolved_ev.msg.data["record_id"] == rec.record_id
    assert req_id in resolved_ev.msg.data["request_ids"]
    # 续采样以 turn_completed 收尾
    assert events2[-1].msg.kind == "turn_completed", f"续跑应完成，实得 {kinds2}"

    # 被挂起的 call_d1 现在有了 function_call_output（gap 补齐）
    fco_ids = {
        it.payload.get("call_id")
        for it in items2
        if it.kind == "function_call_output"
    }
    assert "call_d1" in fco_ids, "被批准的挂起 call 必须补回 function_call_output"


async def test_resume_system_retry(skills_dir, threads_dir):
    """system_retry 挂起 → Resume(action=retry) → 第二次 stream 成功完成。

    第一轮 stream 抛 RateLimitError（可恢复）→ turn 挂起为 SYSTEM_RETRY；
    resume 时 {req_id: {"action": "retry"}} → 续采样走第二个（成功）turn → 完成。
    """
    import taifeng
    from taifeng.llm.client import ModelClient
    from taifeng.llm.errors import RateLimitError
    from taifeng.llm.providers import MockSession, MockTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.submission import Resume

    class _RetryThenOkClient(ModelClient):
        """首次 session 的 stream 抛 RateLimitError；之后成功回放空文本完成。

        模拟「retry 耗尽 → 挂起 → resume 续跑时 provider 已恢复」。
        """

        def __init__(self):  # noqa: ANN204
            self._calls = 0

        def session(self, *, cancel, model=None):  # noqa: ANN001, ANN201
            self._calls += 1
            if self._calls == 1:
                return _RaisingOnceSession(cancel)
            # resume 续跑：正常成功 turn（纯文本，无 tool call → 完成）
            return MockSession(
                turn=MockTurn(
                    text="recovered and done",
                    usage=TokenUsage(input_tokens=6, output_tokens=3),
                ),
                cancel=cancel,
                model=model or "mock-model",
            )

    class _RaisingOnceSession:
        """stream 直接抛 RateLimitError（模拟首轮 provider retry 已耗尽）。"""

        def __init__(self, cancel):  # noqa: ANN001, ANN204
            self._cancel = cancel

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
            pass

        async def stream(self, request):  # noqa: ANN001, ANN201
            raise RateLimitError("rate limited", retry_after_seconds=3.0)
            yield  # pragma: no cover —— 使函数成为 async generator

    skill_md = """---
name: retry-resume-skill
description: system_retry resume entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
max_call_depth: 2
---
# RetryResume
"""
    (skills_dir / "retry-resume-skill").mkdir()
    (skills_dir / "retry-resume-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=_RetryThenOkClient(),
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="retry-resume-e2e", entry_skill_id="retry-resume-skill",
    )

    # === 第一轮：可恢复错误 → 挂起为 SYSTEM_RETRY ===
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await _drain_until_terminal(engine, sub_id)
    # 挂起阶段以独立终结态 turn_suspended 收尾（R3）
    assert events1[-1].msg.kind == "turn_suspended"

    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1
    rec = SuspensionRecord.from_item(suspension_items[0])
    assert rec.pending[0].reason is SuspendReason.SYSTEM_RETRY
    req_id = rec.pending[0].request_id

    # === 第二轮：Resume(action=retry) → 续采样成功完成 ===
    resume_sub = await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"action": "retry"}},
    ))
    events2 = await _drain_until_terminal(engine, resume_sub)
    await pool.close()

    kinds2 = [ev.msg.kind for ev in events2]
    assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
    assert events2[-1].msg.kind == "turn_completed", f"续跑应完成，实得 {kinds2}"


async def test_permission_policy_preapprove_one_shot():
    """预批准:check 命中 call_id 即放行(不触发 prompter),且一次性(只放行一次)。"""
    import pytest

    from taifeng.permission.types import PermissionPolicy, PermissionRequest, SuspendingPrompter
    from taifeng.suspend.signal import SuspendSignal
    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    req = PermissionRequest.for_tool_call(
        "shell_exec", {"cmd": "echo hi"},
        thread_id="t", submission_id="s", entry_skill_id="root",
        turn_index=1, call_chain=("root",),
        extra_metadata={"call_id": "call_X"},
    )
    # 未预批准:仍挂起
    with pytest.raises(SuspendSignal):
        await policy.check(req)
    # 预批准 call_X 后:放行,不挂起
    policy.preapprove("call_X")
    decision = await policy.check(req)
    assert decision.granted is True
    # 一次性:再次 check 又挂起(预批准已被消费)
    with pytest.raises(SuspendSignal):
        await policy.check(req)


# ============================================================
# Task 13 A：顶层公共导出
# ============================================================


def test_public_exports():
    """挂起 / resume 核心原语必须在顶层 taifeng 命名空间可见。

    导出范围对齐 Task 13：suspend 模块原语 + Resume Op 走顶层；
    EventMsg 子类与 SuspendingPrompter 因同族符号本就不在顶层（仅 EventMsg /
    PermissionPolicy 暴露），故只校验 SuspendingPrompter 可从 taifeng.permission 导入。
    """
    import taifeng

    for name in (
        "SuspendReason",
        "PendingRequest",
        "Resume",
        "SuspensionRecord",
        "SuspensionResolver",
        "SuspendSignal",
        "ResolvePlan",
        "ResolveError",
    ):
        assert hasattr(taifeng, name), name
    from taifeng.permission import SuspendingPrompter  # noqa: F401


# ============================================================
# Task 13 B：边界 / 幂等 / cancel / tier-2 重建 resume
# ============================================================


async def _suspend_pool_and_engine(
    skills_dir, threads_dir, *, session_id, client, gated_tool, policy,
):
    """搭建一个会在第一轮挂起（permission ask）的 pool + engine 并返回二者。

    复用 `_build_suspend_skill` 写入 entry skill；调用方负责后续 submit / close。
    """
    import taifeng

    _build_suspend_skill(skills_dir)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[gated_tool],
        permission_policy=policy,
    )
    engine = await pool.get_or_create(
        session_id=session_id, entry_skill_id="suspend-skill",
    )
    return pool, engine


def _suspend_client():
    """构造一个第一轮产出 danger tool call、第二轮纯文本完成的 MockClient。"""
    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage

    return MockClient(turns=[
        MockTurn(
            text="calling danger",
            tool_calls=[{"id": "call_d1", "name": "danger", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
        MockTurn(
            text="approved and done",
            usage=TokenUsage(input_tokens=8, output_tokens=4),
        ),
    ])


async def _suspend_once(pool, engine):
    """提交 UserMessage 触发挂起，返回 (req_id, record)。"""
    import taifeng

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events = await _drain_until_terminal(engine, sub_id)
    # 挂起阶段以独立终结态 turn_suspended 收尾（R3）
    assert events[-1].msg.kind == "turn_suspended"

    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    suspension_items = [it for it in items if it.kind == "suspension"]
    assert len(suspension_items) == 1
    rec = SuspensionRecord.from_item(suspension_items[0])
    # 事件 record_id 必须与落盘 record 对齐（R3 可观测）
    assert events[-1].msg.data["record_id"] == rec.record_id
    return rec.pending[0].request_id, rec


async def test_resume_partial_resolution_rejected(skills_dir, threads_dir):
    """单 pending 挂起 + 提交带多余 request_id 的 Resume → resolver 拒绝。

    本用例覆盖「resolutions 与 record.pending 不匹配」这条边界：提交一个
    包含真实 req_id 之外的多余 key 的 Resume，SuspensionResolver.plan 抛
    ResolveError → engine emit suspension_resolve_rejected，turn 不续跑。
    （两 pending 同批挂起经 MockClient 难构造；resolver 层「缺项 / 多余项」
    已由 test_resolver_rejects_incomplete / _rejects_unknown 单测覆盖，此处
    在 engine 层验证拒绝路径真实触发。）
    """
    import taifeng
    from taifeng.permission.types import PermissionPolicy, SuspendingPrompter

    gated_tool = await _gated_tool_with_call_id_factory()
    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool, engine = await _suspend_pool_and_engine(
        skills_dir, threads_dir, session_id="resume-partial",
        client=_suspend_client(), gated_tool=gated_tool, policy=policy,
    )
    req_id, _ = await _suspend_once(pool, engine)

    # 提交带多余 request_id 的 Resume：resolver 应拒绝（unknown request id）
    resume_sub = await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"granted": True}, "bogus_req": {"granted": True}},
    ))
    events = []
    async for ev in engine.subscribe(resume_sub):
        events.append(ev)
        if ev.msg.kind in (
            "suspension_resolve_rejected", "turn_completed", "turn_failed",
        ):
            break

    # 续跑落盘前读取 items（验证 turn 未续跑：无新 function_call_output）
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    kinds = [ev.msg.kind for ev in events]
    assert "suspension_resolve_rejected" in kinds, f"应拒绝部分 / 多余 resume，实得 {kinds}"
    # turn 不应续跑完成（没有 suspension_resolved，也没有补回挂起 call 的 output）
    assert "suspension_resolved" not in kinds
    fco_ids = {
        it.payload.get("call_id") for it in items
        if it.kind == "function_call_output"
    }
    assert "call_d1" not in fco_ids, "被拒绝的 resume 不得补回 function_call_output"


async def test_duplicate_resume_idempotent(skills_dir, threads_dir):
    """重复 Resume 幂等：首次成功后再次提交同一 Resume → no_active_suspension 拒绝。

    record 在首次 resume 时被 resolved-marker 消费；二次 _find_active_suspension
    返回 None → suspension_resolve_rejected(reason=no_active_suspension)，
    且不会二次执行 tool（不崩溃）。
    """
    import taifeng
    from taifeng.permission.types import PermissionPolicy, SuspendingPrompter

    gated_tool = await _gated_tool_with_call_id_factory()
    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool, engine = await _suspend_pool_and_engine(
        skills_dir, threads_dir, session_id="resume-dup",
        client=_suspend_client(), gated_tool=gated_tool, policy=policy,
    )
    req_id, _ = await _suspend_once(pool, engine)

    # 首次 Resume：成功续跑完成
    resume_sub = await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"granted": True}},
    ))
    events1 = await _drain_until_terminal(engine, resume_sub)
    assert "suspension_resolved" in [ev.msg.kind for ev in events1]
    assert events1[-1].msg.kind == "turn_completed"

    # 首次续跑后挂起 call 已补回 output（计 1 条）
    items1 = [it async for it in await pool.store.load_thread(engine.thread_id)]
    fco_count_1 = sum(
        1 for it in items1
        if it.kind == "function_call_output" and it.payload.get("call_id") == "call_d1"
    )
    assert fco_count_1 == 1

    # 二次提交相同 Resume：record 已被消费 → no_active_suspension 拒绝
    resume_sub2 = await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"granted": True}},
    ))
    events2 = []
    async for ev in engine.subscribe(resume_sub2):
        events2.append(ev)
        if ev.msg.kind in (
            "suspension_resolve_rejected", "turn_completed", "turn_failed",
        ):
            break

    items2 = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    kinds2 = [ev.msg.kind for ev in events2]
    rej = next(
        ev for ev in events2 if ev.msg.kind == "suspension_resolve_rejected"
    )
    assert rej.msg.data["reason"] == "no_active_suspension", f"实得 {kinds2}"
    # 幂等：未再次执行 tool（call_d1 的 output 仍只有 1 条）
    fco_count_2 = sum(
        1 for it in items2
        if it.kind == "function_call_output" and it.payload.get("call_id") == "call_d1"
    )
    assert fco_count_2 == 1, "重复 resume 不得二次执行 tool"


async def test_resume_unknown_thread_or_no_suspension(skills_dir, threads_dir):
    """对一个无活跃挂起的 thread 提交 Resume → no_active_suspension 拒绝（不崩溃）。

    引擎单 thread，故用 engine 自身的 thread_id；该 thread 从未挂起 →
    _find_active_suspension 返回 None → suspension_resolve_rejected。
    """
    import taifeng
    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage

    # 一个纯文本（不挂起）的 entry skill：直接完成,无挂起
    skill_md = """---
name: plain-skill
description: plain entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
max_call_depth: 2
---
# Plain
"""
    (skills_dir / "plain-skill").mkdir()
    (skills_dir / "plain-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")

    client = MockClient(turns=[
        MockTurn(text="done", usage=TokenUsage(input_tokens=4, output_tokens=2)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="no-suspension", entry_skill_id="plain-skill",
    )

    # 对无挂起的 thread 提交 Resume → 拒绝
    resume_sub = await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id, resolutions={"x": {}},
    ))
    events = []
    async for ev in engine.subscribe(resume_sub):
        events.append(ev)
        if ev.msg.kind in (
            "suspension_resolve_rejected", "turn_completed", "turn_failed",
        ):
            break
    await pool.close()

    rej = next(
        ev for ev in events if ev.msg.kind == "suspension_resolve_rejected"
    )
    assert rej.msg.data["reason"] == "no_active_suspension"


async def test_cancel_clears_suspension_then_resume_rejected(skills_dir, threads_dir):
    """R4 闭环：挂起后 CancelTurn 该挂起 turn → 活跃挂起被丢弃 → 后续 Resume 被拒。

    挂起 turn 在挂起点已以 end_reason=suspended 收尾，其 _pending 条目已退栈；
    因此 CancelTurn 找不到 live pending 目标，但必须按 submission_id 匹配活跃挂起
    record 并落 resolved-marker 丢弃之（与 _handle_resume 同机制）。丢弃后：
      - _find_active_suspension 不再返回该 record；
      - 后续 Resume 命中 no_active_suspension 被拒（suspension_resolve_rejected）；
      - 被挂起的 tool 不被执行（无 call_d1 的 function_call_output 回填）。

    取代旧的「cancel 对挂起 turn 是 no-op」断言（Task 13 占位，已被本任务闭环）。
    """
    import taifeng
    from taifeng.loop.submission import CancelTurn
    from taifeng.permission.types import PermissionPolicy, SuspendingPrompter

    gated_tool = await _gated_tool_with_call_id_factory()
    policy = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool, engine = await _suspend_pool_and_engine(
        skills_dir, threads_dir, session_id="cancel-suspend",
        client=_suspend_client(), gated_tool=gated_tool, policy=policy,
    )

    # 1. 触发挂起，捕获挂起 turn 的 submission id 与持久化 record 的 request_id
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await _drain_until_terminal(engine, sub_id)
    # 挂起阶段以独立终结态 turn_suspended 收尾（R3）
    assert events1[-1].msg.kind == "turn_suspended"

    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    rec = SuspensionRecord.from_item(
        next(it for it in items if it.kind == "suspension")
    )
    req_id = rec.pending[0].request_id
    # 挂起 record 的 submission_id 应等于触发挂起的 UserMessage 的 sub_id
    assert rec.submission_id == sub_id

    # 2. CancelTurn(该挂起 sub_id) → 应丢弃活跃挂起（落 resolved-marker）
    cancel_sub = await engine.submit(CancelTurn(submission_id=sub_id))
    # CancelTurn 无终态事件流，仅 emit EngineLog；以排空一次确保已被主循环处理
    async for ev in engine.subscribe(cancel_sub):
        if ev.msg.kind == "engine_log":
            break

    # 3. Resume → 因挂起已被 cancel 丢弃，命中 no_active_suspension 被拒
    resume_sub = await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"granted": True}},
    ))
    events2 = []
    async for ev in engine.subscribe(resume_sub):
        events2.append(ev)
        if ev.msg.kind in (
            "suspension_resolve_rejected", "turn_completed", "turn_failed",
        ):
            break

    items2 = [it async for it in await pool.store.load_thread(engine.thread_id)]
    await pool.close()

    kinds2 = [ev.msg.kind for ev in events2]
    # 4a. Resume 被拒（reason=no_active_suspension —— cancel 已丢弃挂起）
    rej = next(
        (ev for ev in events2 if ev.msg.kind == "suspension_resolve_rejected"),
        None,
    )
    assert rej is not None, f"cancel 后 resume 应被拒，实得 {kinds2}"
    assert rej.msg.data["reason"] == "no_active_suspension", f"实得 {kinds2}"
    # 4b. 挂起 tool 未被执行（无 call_d1 的 function_call_output 回填）
    fco_d1 = [
        it for it in items2
        if it.kind == "function_call_output" and it.payload.get("call_id") == "call_d1"
    ]
    assert not fco_d1, "被 cancel 丢弃的挂起不得回填 function_call_output"


async def test_tier2_rebuild_resume(skills_dir, threads_dir):
    """R5（头条）：进程退出 + 重建后跨实例 resume。

    1. pool#1：UserMessage 触发 permission 挂起 → 落 suspension item，记 thread_id / req_id。
    2. 完全释放 pool#1（模拟实例销毁）。
    3. pool#2（同 threads_dir）：get_or_create(resume_thread_id=<thread_id>) 物化历史。
    4. 对新 engine 提交 Resume → 经重建的 history 经 _find_active_suspension 找到挂起，
       补齐 gap、续采样完成 → 证明跨进程 resume。
    """
    import taifeng
    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.permission.types import PermissionPolicy, SuspendingPrompter

    # === 实例#1：触发挂起并落盘 ===
    gated_tool_1 = await _gated_tool_with_call_id_factory()
    policy_1 = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    pool1, engine1 = await _suspend_pool_and_engine(
        skills_dir, threads_dir, session_id="tier2-orig",
        client=_suspend_client(), gated_tool=gated_tool_1, policy=policy_1,
    )
    req_id, rec = await _suspend_once(pool1, engine1)
    thread_id = engine1.thread_id

    # === 完全释放实例#1（模拟进程退出 / 实例销毁）===
    await pool1.close()
    del pool1, engine1, policy_1, gated_tool_1

    # === 实例#2：同 threads_dir 全新构造，从持久化 thread 续接 ===
    gated_tool_2 = await _gated_tool_with_call_id_factory()
    policy_2 = PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter())
    # resume 续跑只需要「成功完成」的一轮（无需再产出 tool call）
    client2 = MockClient(turns=[
        MockTurn(text="recovered and done", usage=TokenUsage(input_tokens=6, output_tokens=3)),
    ])
    # 注意：suspend-skill 已在实例#1 setup 时写入磁盘，重建实例直接复用，无需再写。
    pool2 = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client2, compressors=[], extra_tools=[gated_tool_2],
        permission_policy=policy_2,
    )
    # 用全新 session_id + resume_thread_id 续接同一持久化 thread
    engine2 = await pool2.get_or_create(
        session_id="tier2-rebuilt", entry_skill_id="suspend-skill",
        resume_thread_id=thread_id,
    )
    assert engine2.thread_id == thread_id, "重建 engine 必须复用原 thread_id"

    # === 对重建实例提交 Resume → 跨进程续跑 ===
    resume_sub = await engine2.submit(taifeng.Resume(
        thread_id=thread_id, resolutions={req_id: {"granted": True}},
    ))
    events = await _drain_until_terminal(engine2, resume_sub)

    items = [it async for it in await pool2.store.load_thread(thread_id)]
    await pool2.close()

    kinds = [ev.msg.kind for ev in events]
    assert "suspension_resolved" in kinds, f"跨进程 resume 未见 resolved，实得 {kinds}"
    resolved_ev = next(ev for ev in events if ev.msg.kind == "suspension_resolved")
    assert resolved_ev.msg.data["record_id"] == rec.record_id
    assert events[-1].msg.kind == "turn_completed", f"跨进程续跑应完成，实得 {kinds}"
    # gap 补齐：被批准的挂起 call 现在有 output
    fco_ids = {
        it.payload.get("call_id") for it in items
        if it.kind == "function_call_output"
    }
    assert "call_d1" in fco_ids, "跨进程 resume 必须补回 function_call_output"
