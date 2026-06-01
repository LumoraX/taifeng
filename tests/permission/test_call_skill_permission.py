"""T4 + T5: call_skill 接 PermissionPolicy + 新 Hook lifecycle + EventMsg。

覆盖 spec ``skill-dispatch``：
    - DispatchPolicy 通过但 PermissionPolicy 拒绝 → ToolResult.error
    - PreDispatch hook 拒绝 → ToolResult.error；PermissionPolicy 不被调用
    - PostDispatch hook 仅审计；deny 不影响 ToolResult
    - 上下文字段（thread_id / call_chain / entry_skill_id）正确透传
    - PermissionPolicy=None / hook_runner=None 时跳过对应阶段（向后兼容）

覆盖 spec ``permission-gate`` 集成：
    - typed request 字段在 prompter 中被读取
    - hook 顺序：pre_skill_dispatch 在 PermissionPolicy 之前
    - PostSkillDispatchHook.success 反映子 turn 成败
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from taifeng.skill import CallStack, DispatchPolicy, SkillDefinition
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.builtins import make_call_skill_tool
from taifeng.tool.spec import ToolContext, ToolResult


# ====================================================================
# 测试 fixtures —— 手工构造 skills / snapshot / 假 dispatcher
# ====================================================================


def _mk_skill(
    sid: str,
    *,
    child: frozenset[str] = frozenset(),
    entry: bool = False,
    max_depth: int = 6,
) -> SkillDefinition:
    return SkillDefinition(
        id=sid,
        name=sid,
        description=f"skill {sid}",
        version="1",
        body="",
        body_path=Path("/tmp") / sid,
        type="composite" if (child or entry) else "atomic",
        entry=entry,
        child_skills=child,
        max_call_depth=max_depth,
    )


class _FakeDispatcher:
    """伪 dispatcher：记录调用 + emit 事件 + 返回预设 ToolResult。"""

    def __init__(
        self,
        *,
        sub_result: ToolResult | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.emitted: list[EventMsg] = []
        self._sub_result = sub_result or ToolResult.ok(
            "sub-ok", sub_thread_id="t-sub-1"
        )

    async def run_sub_skill(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ) -> ToolResult:
        self.calls.append(
            {
                "target": target.id,
                "args": arguments,
                "depth": parent_stack.depth,
                "path": parent_stack.path(),
            }
        )
        return self._sub_result

    async def _emit(self, msg: Any) -> None:
        """模拟 TurnRunner._emit 签名：收 msg 实例后包 EventMsg。

        生产路径上 TurnRunner._emit(msg) 内部做
        ``EventMsg(submission_id=..., msg=msg)`` 再 forward；测试 fake 同样
        必须 wrap，否则下游断言 ``ev.msg.kind`` 会拿不到属性。
        """
        self.emitted.append(EventMsg(submission_id="sub-fake", msg=msg))


def _make_snapshot(skills: list[SkillDefinition]) -> SkillSnapshot:
    """构造一个简单的 SkillSnapshot。

    reachable_graph 简单实现：所有 skill 互通（测试场景够用）。
    """
    all_ids = frozenset(s.id for s in skills)
    reachable = {s.id: all_ids for s in skills}
    return SkillSnapshot(
        version=1,
        skills=tuple(skills),
        reachable_graph=reachable,
    )


def _make_ctx(
    *,
    dispatcher: _FakeDispatcher,
    snapshot: SkillSnapshot,
    caller: SkillDefinition,
    stack: CallStack,
    permission_policy: PermissionPolicy | None = None,
    hook_runner: HookRunner | None = None,
    request_metadata: dict | None = None,
    submission_id: str = "sub-1",
    entry_skill_id: str | None = None,
    turn_index: int = 1,
    thread_id: str = "t-1",
) -> ToolContext:
    return ToolContext(
        call_id="tc-call-skill",
        cancel=CancellationToken(),
        thread_id=thread_id,
        extras={
            "skill_snapshot": snapshot,
            "dispatch_policy": DispatchPolicy(),
            "call_stack": stack,
            "current_skill": caller,
            "dispatcher": dispatcher,
            "submission_id": submission_id,
            "entry_skill_id": entry_skill_id or caller.id,
            "permission_policy": permission_policy,
            "hook_runner": hook_runner,
            "request_metadata": request_metadata or {},
            "turn_index": turn_index,
        },
    )


# ====================================================================
# T4-1: 顺序：DispatchPolicy 通过但 PermissionPolicy 拒绝
# ====================================================================


@pytest.mark.asyncio
async def test_dispatch_passes_then_permission_denies() -> None:
    """白名单 OK 但 PermissionRule(skill_dispatch, deny) → ToolResult.error。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                scope="skill_dispatch", target_pattern="c", mode="deny",
                reason="tenant_blocked",
            ),
        ],
        default_mode="allow",
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert result.is_error
    assert "skill_dispatch_denied" in result.output
    assert "tenant_blocked" in result.output
    # SHALL NOT 启动子 TurnRunner
    assert len(dispatcher.calls) == 0


# ====================================================================
# T4-2: PreDispatch hook 拒绝 + PermissionPolicy 不被调用
# ====================================================================


@pytest.mark.asyncio
async def test_pre_skill_dispatch_hook_can_block() -> None:
    """pre_skill_dispatch hook deny → ToolResult.error 含 hook reason。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    reg = HookRegistry()
    hook_calls: list[str] = []

    async def deny_hook(hook, ctx) -> HookDecision:
        hook_calls.append("pre")
        return HookDecision.deny("free_tier_blocked")

    reg.register("pre_skill_dispatch", deny_hook)
    runner = HookRunner(reg)

    perm_called = False

    async def cb(req: PermissionRequest) -> PermissionDecision:
        nonlocal perm_called
        perm_called = True
        return PermissionDecision.allow(reason="should_not_be_called")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy, hook_runner=runner,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert result.is_error
    assert "skill_dispatch_hook_denied" in result.output
    assert "free_tier_blocked" in result.output
    assert hook_calls == ["pre"]
    assert perm_called is False, "PermissionPolicy 不该在 hook deny 后被调用"
    assert len(dispatcher.calls) == 0


# ====================================================================
# T4-3: PostDispatch hook 仅审计；deny / 异常不改 ToolResult
# ====================================================================


@pytest.mark.asyncio
async def test_post_skill_dispatch_hook_audit_only() -> None:
    """post_skill_dispatch hook 返回 deny 不影响 ToolResult（仅审计）。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    reg = HookRegistry()
    post_called: list[Any] = []

    async def deny_post(hook, ctx) -> HookDecision:
        post_called.append(hook)
        return HookDecision.deny("post_thinks_no")

    reg.register("post_skill_dispatch", deny_post)
    runner = HookRunner(reg)

    dispatcher = _FakeDispatcher(
        sub_result=ToolResult.ok("子 skill 成功输出", sub_thread_id="t-sub-X"),
    )
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        hook_runner=runner,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    # 子 turn 实际结果保留 —— 不被 post hook deny 覆盖
    assert not result.is_error
    assert result.output == "子 skill 成功输出"
    assert len(post_called) == 1
    # success 反映子 turn 实际结果
    assert post_called[0].success is True
    assert post_called[0].sub_thread_id == "t-sub-X"


# ====================================================================
# T4-4: typed request 字段被 prompter 读取（含 call_chain 多层）
# ====================================================================


@pytest.mark.asyncio
async def test_typed_request_fields_populated() -> None:
    """spy CallbackPrompter 收到的 request 含 thread_id / call_chain / entry_skill_id 全部非空。"""
    # 模拟 A → B 调用栈，dispatch C
    caller = _mk_skill("B", child=frozenset({"C"}))
    target = _mk_skill("C")
    entry = _mk_skill("A", child=frozenset({"B"}), entry=True)
    snapshot = _make_snapshot([entry, caller, target])
    stack = CallStack().push("A", "1").push("B", "2")

    captured: list[PermissionRequest] = []

    async def cb(req: PermissionRequest) -> PermissionDecision:
        captured.append(req)
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
        entry_skill_id="A", request_metadata={"x_corr": "v-7"}, turn_index=5,
        submission_id="sub-99", thread_id="th-42",
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "C"}, ctx)
    assert not result.is_error

    assert len(captured) == 1
    req = captured[0]
    assert req.scope == "skill_dispatch"
    assert req.target == "C"
    assert req.thread_id == "th-42"
    assert req.submission_id == "sub-99"
    assert req.entry_skill_id == "A"
    assert req.call_chain == ("A", "B")
    assert req.turn_index == 5
    # 业务侧不透明上下文经 request_metadata 合并进 metadata（无业务命名字段，R1）；
    # call_id 由 builtin 透传(Task 8)→ SuspendingPrompter 据此填 related_call_id
    assert req.metadata == {
        "caller_skill_id": "B", "x_corr": "v-7", "call_id": "tc-call-skill",
    }


# ====================================================================
# T5: EventMsg —— permission_denied / hook_denied 事件
# ====================================================================


@pytest.mark.asyncio
async def test_emits_permission_denied_event() -> None:
    """PermissionPolicy 拒绝时 emit ``skill_dispatch_permission_denied`` 事件。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                scope="skill_dispatch", target_pattern="c", mode="deny",
                reason="quota_exhausted",
            ),
        ],
        default_mode="allow",
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "c"}, ctx)
    kinds = [ev.msg.kind for ev in dispatcher.emitted]
    assert "skill_dispatch_permission_denied" in kinds
    ev = next(
        e for e in dispatcher.emitted
        if e.msg.kind == "skill_dispatch_permission_denied"
    )
    assert ev.msg.data["target_skill_id"] == "c"
    assert ev.msg.data["caller_skill_id"] == "p"
    assert ev.msg.data["call_chain"] == ["p"]
    assert "quota_exhausted" in ev.msg.data["reason"]


@pytest.mark.asyncio
async def test_emits_hook_denied_event() -> None:
    """pre_skill_dispatch hook 拒绝时 emit ``skill_dispatch_hook_denied`` 事件。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    reg = HookRegistry()

    async def deny(hook, ctx) -> HookDecision:
        return HookDecision.deny("audit_blocked")

    reg.register("pre_skill_dispatch", deny)
    runner = HookRunner(reg)

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        hook_runner=runner,
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "c"}, ctx)
    kinds = [ev.msg.kind for ev in dispatcher.emitted]
    assert "skill_dispatch_hook_denied" in kinds


# ====================================================================
# T6: 边界用例
# ====================================================================


@pytest.mark.asyncio
async def test_no_permission_policy_skips_check() -> None:
    """ctx.extras['permission_policy'] = None 时跳过 step 3（向后兼容）。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=None,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert not result.is_error
    assert len(dispatcher.calls) == 1
    # 未走 PermissionPolicy → 不发对应 event
    kinds = [ev.msg.kind for ev in dispatcher.emitted]
    assert "skill_dispatch_permission_denied" not in kinds


@pytest.mark.asyncio
async def test_post_hook_triggered_on_sub_turn_failure() -> None:
    """子 turn 失败时 post_skill_dispatch hook 也被调用，success=False。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    reg = HookRegistry()
    captured: list[Any] = []

    async def audit(hook, ctx) -> HookDecision:
        captured.append(hook)
        return HookDecision.ok()

    reg.register("post_skill_dispatch", audit)
    runner = HookRunner(reg)

    dispatcher = _FakeDispatcher(
        sub_result=ToolResult.error("sub_skill_failed: oops", sub_thread_id="t-x"),
    )
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        hook_runner=runner,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert result.is_error  # 子 turn 失败传到外层
    assert len(captured) == 1
    assert captured[0].success is False
    assert captured[0].sub_thread_id == "t-x"


@pytest.mark.asyncio
async def test_hook_order_pre_before_permission() -> None:
    """T6-3: hook 顺序 —— pre_skill_dispatch 先跑，再 PermissionPolicy（与 ToolCallRuntime 一致）。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    order: list[str] = []

    reg = HookRegistry()

    async def pre(hook, ctx) -> HookDecision:
        order.append("pre_hook")
        return HookDecision.ok()

    async def post(hook, ctx) -> HookDecision:
        order.append("post_hook")
        return HookDecision.ok()

    reg.register("pre_skill_dispatch", pre)
    reg.register("post_skill_dispatch", post)
    runner = HookRunner(reg)

    async def cb(req: PermissionRequest) -> PermissionDecision:
        order.append("permission_check")
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )

    dispatcher = _FakeDispatcher()
    # 包装 dispatcher.run_sub_skill 记录顺序
    original_run = dispatcher.run_sub_skill

    async def tracked_run(**kwargs):
        order.append("sub_turn")
        return await original_run(**kwargs)

    dispatcher.run_sub_skill = tracked_run  # type: ignore[method-assign]

    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy, hook_runner=runner,
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "c"}, ctx)
    assert order == [
        "pre_hook",
        "permission_check",
        "sub_turn",
        "post_hook",
    ]


@pytest.mark.asyncio
async def test_dispatch_policy_failure_does_not_call_hook_or_permission() -> None:
    """DispatchPolicy 拒绝（结构性）→ 直接返回；hook 和 permission 都不跑。"""
    caller = _mk_skill("p", child=frozenset({"OTHER"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    pre_called = False
    perm_called = False

    reg = HookRegistry()

    async def pre(hook, ctx) -> HookDecision:
        nonlocal pre_called
        pre_called = True
        return HookDecision.ok()

    reg.register("pre_skill_dispatch", pre)
    runner = HookRunner(reg)

    async def cb(req: PermissionRequest) -> PermissionDecision:
        nonlocal perm_called
        perm_called = True
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy, hook_runner=runner,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert result.is_error
    assert "dispatch_rejected" in result.output
    assert "not_in_whitelist" in result.output
    assert pre_called is False
    assert perm_called is False


@pytest.mark.asyncio
async def test_request_metadata_optional_default_empty() -> None:
    """request_metadata 未注入时 PermissionRequest.metadata 不含业务键（仅工厂默认）。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    captured: list[PermissionRequest] = []

    async def cb(req: PermissionRequest) -> PermissionDecision:
        captured.append(req)
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy, request_metadata=None,
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "c"}, ctx)
    # 未注入 request_metadata → metadata 含 skill_dispatch 工厂默认的 caller_skill_id
    # 及 builtin 透传的 call_id(Task 8:供挂起时配对 related_call_id)
    assert captured[0].metadata == {"caller_skill_id": "p", "call_id": "tc-call-skill"}


@pytest.mark.asyncio
async def test_pre_hook_receives_full_ctx_fields() -> None:
    """pre_skill_dispatch hook 接收完整 ctx 字段（target / args / caller / chain / depth）。"""
    caller = _mk_skill("B", child=frozenset({"C"}))
    target = _mk_skill("C")
    entry = _mk_skill("A", child=frozenset({"B"}), entry=True)
    snapshot = _make_snapshot([entry, caller, target])
    stack = CallStack().push("A", "1").push("B", "2")

    captured: list[Any] = []
    reg = HookRegistry()

    async def pre(hook, ctx) -> HookDecision:
        captured.append(hook)
        return HookDecision.ok()

    reg.register("pre_skill_dispatch", pre)
    runner = HookRunner(reg)

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        hook_runner=runner, entry_skill_id="A",
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "C", "args": {"x": 1}}, ctx)
    assert len(captured) == 1
    hook = captured[0]
    assert hook.target_skill_id == "C"
    assert hook.args == {"x": 1}
    assert hook.caller_skill_id == "B"
    assert hook.call_chain == ("A", "B")
    assert hook.depth == 3


@pytest.mark.asyncio
async def test_post_hook_output_preview_truncated() -> None:
    """T6: PostSkillDispatchHook.output_preview 按 SKILL_OUTPUT_PREVIEW_LIMIT 截断。

    避免 hook 数据爆炸（spec hooks: ``最多 1KB 截断``）。
    """
    from taifeng.hooks import SKILL_OUTPUT_PREVIEW_LIMIT

    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "1")

    reg = HookRegistry()
    captured: list[Any] = []

    async def audit(hook, ctx) -> HookDecision:
        captured.append(hook)
        return HookDecision.ok()

    reg.register("post_skill_dispatch", audit)
    runner = HookRunner(reg)

    big_output = "x" * (SKILL_OUTPUT_PREVIEW_LIMIT * 3)
    dispatcher = _FakeDispatcher(
        sub_result=ToolResult.ok(big_output, sub_thread_id="t1"),
    )
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        hook_runner=runner,
    )

    tool = make_call_skill_tool()
    await tool.handler({"skill_id": "c"}, ctx)
    assert len(captured) == 1
    # output_preview 字节长度受限
    encoded_len = len(captured[0].output_preview.encode("utf-8"))
    assert encoded_len <= SKILL_OUTPUT_PREVIEW_LIMIT
