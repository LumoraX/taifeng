"""call-skill-reason-field: 验证 call_skill 工具 reason 字段端到端透传。

覆盖 spec ``skill-dispatch`` delta（含 2026-05-27 AMENDMENT）：
    - schema 含 **required** reason 字段（AMENDMENT 之后从 optional 改 required）
    - handler 提取 reason 透传到 PermissionRequest
    - handler 防御性兜底：args 缺 reason 时仍能工作（非 schema 路径）
    - skill_dispatched.data["reason"] 携带 LLM 自陈
    - skill_dispatch_permission_denied.data["request_reason"] 携带 LLM 自陈
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.skill import CallStack, DispatchPolicy, SkillDefinition
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.builtins import make_call_skill_tool
from taifeng.tool.builtins.call_skill import CALL_SKILL_SCHEMA
from taifeng.tool.spec import ToolContext, ToolResult

# ====================================================================
# fixtures —— 复用 test_call_skill_permission.py 的 minimal stub
# ====================================================================


def _mk_skill(
    sid: str,
    *,
    child: frozenset[str] = frozenset(),
    entry: bool = False,
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
        max_call_depth=6,
    )


class _FakeDispatcher:
    def __init__(self) -> None:
        self.emitted: list[EventMsg] = []
        self.last_ctx_extras: dict[str, Any] | None = None

    async def run_sub_skill(
        self,
        *,
        target: SkillDefinition,
        arguments: dict[str, Any],
        parent_stack: CallStack,
        ctx: ToolContext,
    ) -> ToolResult:
        # 记录 ctx.extras 让测试断言 dispatch_reason 透传
        self.last_ctx_extras = dict(ctx.extras)
        return ToolResult.ok("sub-ok", sub_thread_id="t-sub-1")

    async def _emit(self, msg: Any) -> None:
        # 模拟 TurnRunner._emit 签名：收 msg 实例后包 EventMsg 再存
        self.emitted.append(EventMsg(submission_id="sub-fake", msg=msg))


def _make_snapshot(skills: list[SkillDefinition]) -> SkillSnapshot:
    all_ids = frozenset(s.id for s in skills)
    reachable = {s.id: all_ids for s in skills}
    return SkillSnapshot(
        version=1, skills=tuple(skills), reachable_graph=reachable,
    )


def _make_ctx(
    *,
    dispatcher: _FakeDispatcher,
    snapshot: SkillSnapshot,
    caller: SkillDefinition,
    stack: CallStack,
    permission_policy: PermissionPolicy | None = None,
) -> ToolContext:
    return ToolContext(
        call_id="tc-call-skill",
        cancel=CancellationToken(),
        thread_id="t-1",
        extras={
            "skill_snapshot": snapshot,
            "dispatch_policy": DispatchPolicy(),
            "call_stack": stack,
            "current_skill": caller,
            "dispatcher": dispatcher,
            "submission_id": "sub-1",
            "entry_skill_id": caller.id,
            "permission_policy": permission_policy,
            "turn_index": 1,
        },
    )


# ====================================================================
# 1. schema：reason 字段存在且 **required**（AMENDMENT 2026-05-27）
# ====================================================================


def test_schema_requires_reason() -> None:
    """AMENDMENT 2026-05-27: reason 从 optional 改为 required。

    handler 仍保留 args.get 防御性兜底（非 schema 路径的 unit test 可绕过），
    但 LLM 走 provider 的 schema 校验时 reason 必填。
    """
    assert "reason" in CALL_SKILL_SCHEMA["properties"]
    assert CALL_SKILL_SCHEMA["properties"]["reason"]["type"] == "string"
    assert "reason" in CALL_SKILL_SCHEMA["required"]
    assert "skill_id" in CALL_SKILL_SCHEMA["required"]


# ====================================================================
# 2. handler：reason 透传到 PermissionRequest
# ====================================================================


@pytest.mark.asyncio
async def test_handler_extracts_reason_passes_to_permission_request() -> None:
    """LLM 传 reason → prompter 收到 PermissionRequest.reason == 该值。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    captured: dict[str, Any] = {}

    async def cb(req: PermissionRequest) -> PermissionDecision:
        captured["reason"] = req.reason
        captured["target"] = req.target
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )
    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    result = await tool.handler(
        {
            "skill_id": "c",
            "reason": "需要 security-scanner 审查这段处理用户输入的 SQL 拼接代码",
        },
        ctx,
    )
    assert not result.is_error
    assert captured["reason"] == "需要 security-scanner 审查这段处理用户输入的 SQL 拼接代码"
    assert captured["target"] == "c"


# ====================================================================
# 3. handler：未提供 reason 时缺省空串
# ====================================================================


@pytest.mark.asyncio
async def test_handler_defaults_reason_to_empty_when_missing() -> None:
    """schema required 后该路径只在非 schema 校验场景出现（直接 unit test 调
    handler 时 schema 不强制）。handler 自身仍 args.get 防御性兜底空串，
    确保不抛 KeyError。生产路径下 LLM 走 provider schema 校验时 reason 必填。
    """
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    captured_reason: list[str] = []

    async def cb(req: PermissionRequest) -> PermissionDecision:
        captured_reason.append(req.reason)
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )
    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    result = await tool.handler({"skill_id": "c"}, ctx)
    assert not result.is_error
    assert captured_reason == [""]


# ====================================================================
# 4. handler：reason 类型错误时返回 bad_args
# ====================================================================


@pytest.mark.asyncio
async def test_handler_rejects_non_string_reason() -> None:
    """reason 不是字符串 → ToolResult.error('bad_args')，不调 prompter。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

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
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    # 故意传 int 让 schema/handler 拒绝
    result = await tool.handler({"skill_id": "c", "reason": 123}, ctx)
    assert result.is_error
    assert "reason must be string" in result.output
    assert perm_called is False


# ====================================================================
# 5. dispatch_reason 写入 sub_ctx.extras 供 TurnRunner emit SkillDispatched
# ====================================================================


@pytest.mark.asyncio
async def test_dispatch_reason_written_to_sub_ctx_extras() -> None:
    """call_skill 在 run_sub_skill 前把 dispatch_reason 注入 sub_ctx.extras。

    TurnRunner.run_sub_skill 读这个 key 写入 SkillDispatched.data["reason"]。
    这个测试用 _FakeDispatcher 记录 ctx.extras 验证透传。
    """
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
    )

    tool = make_call_skill_tool()
    result = await tool.handler(
        {"skill_id": "c", "reason": "派发理由 XYZ"}, ctx,
    )
    assert not result.is_error
    assert dispatcher.last_ctx_extras is not None
    assert dispatcher.last_ctx_extras["dispatch_reason"] == "派发理由 XYZ"


# ====================================================================
# 6. permission_denied event 含 request_reason 字段
# ====================================================================


@pytest.mark.asyncio
async def test_permission_denied_event_carries_request_reason() -> None:
    """policy deny 时 EventMsg.data 同时含 reason (policy) 与 request_reason (LLM)。"""
    caller = _mk_skill("p", child=frozenset({"c"}), entry=True)
    target = _mk_skill("c")
    snapshot = _make_snapshot([caller, target])
    stack = CallStack().push("p", "call_p")

    async def cb(req: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny(reason="policy_blocked_for_demo")

    policy = PermissionPolicy(
        default_mode="ask", prompter=CallbackPrompter(cb),
    )
    dispatcher = _FakeDispatcher()
    ctx = _make_ctx(
        dispatcher=dispatcher, snapshot=snapshot, caller=caller, stack=stack,
        permission_policy=policy,
    )

    tool = make_call_skill_tool()
    result = await tool.handler(
        {"skill_id": "c", "reason": "LLM 自陈理由 ABC"}, ctx,
    )
    assert result.is_error
    assert "skill_dispatch_denied" in result.output

    # 找 skill_dispatch_permission_denied 事件
    denied_events = [
        ev for ev in dispatcher.emitted
        if ev.msg.kind == "skill_dispatch_permission_denied"
    ]
    assert len(denied_events) == 1
    data = denied_events[0].msg.data
    assert data["reason"] == "policy_blocked_for_demo"
    assert data["request_reason"] == "LLM 自陈理由 ABC"
