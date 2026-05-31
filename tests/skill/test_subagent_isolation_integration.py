"""TurnRunner.run_sub_skill 根据 subagent_approval_mode wire 包装类（G3 T3 + T4）。

覆盖 spec ``skill-dispatch`` ADDED Requirements:
    - "子 TurnRunner 根据 mode 决定 permission_policy 处理"
    - "Emit subagent_policy_overridden event 当 wrapper 创建"

策略：直接构造 TurnRunner 并调 run_sub_skill —— 避开 EnginePool 完整启动开销。
通过 monkey-patch sub_runner 的 ``run`` 让子 turn 不真跑、只记录初始 permission_policy。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg
from taifeng.loop.turn import TurnOutcome, TurnRunner
from taifeng.llm.types import TokenUsage
from taifeng.permission.types import PermissionPolicy
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import (
    CallStack,
    DispatchPolicy,
    _SubagentAutoDecisionPolicy,
)
from taifeng.tool.spec import ToolContext


def _make_skill(skill_id: str, *, entry: bool = False) -> SkillDefinition:
    children = frozenset({"leaf"} if not entry else {"sub"})
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description=f"{skill_id} description",
        version="1.0.0",
        body="# body",
        body_path=Path(f"/tmp/skills/{skill_id}/SKILL.md"),
        type="composite",
        entry=entry,
        model="mock-model" if entry else None,
        child_skills=children,
        tool_names=frozenset(),
        max_call_depth=3,
    )


@pytest.fixture
def captured_sub_policies(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Monkey-patch TurnRunner.run 捕获子 TurnRunner 的 permission_policy 字段。"""
    captured: list[Any] = []

    original_run = TurnRunner.run

    async def _capturing_run(self: TurnRunner) -> TurnOutcome:
        # 仅在 sub TurnRunner（thread_id 以 subskill 开头）记录
        if "sub" in self.thread_id or self.entry_skill.id == "sub":
            captured.append(self.permission_policy)
        # 走原 run 但快速结束（不靠 LLM）
        return TurnOutcome(
            success=True,
            iterations=1,
            duration_ms=0,
            usage=TokenUsage(),
            final_text="sub done",
            end_reason="completed",
        )

    monkeypatch.setattr(TurnRunner, "run", _capturing_run)
    return captured


def _make_parent_runner(
    *,
    dispatch_policy: DispatchPolicy,
    permission_policy: Any,
    emitted: list[EventMsg],
) -> TurnRunner:
    parent_skill = _make_skill("entry", entry=True)
    sub_skill = _make_skill("sub")
    snapshot = MagicMock()
    snapshot.get.return_value = sub_skill
    snapshot.reachable_from.return_value = {"entry", "sub"}

    # store mock：create_thread 返回固定 id；append 不做事
    store = MagicMock()
    store.create_thread = AsyncMock(return_value="thr_sub_abc")
    store.append = AsyncMock(return_value=None)

    async def _emit(ev: EventMsg) -> None:
        emitted.append(ev)

    runner = TurnRunner(
        entry_skill=parent_skill,
        snapshot=snapshot,
        model_client=MagicMock(),
        tool_runtime=MagicMock(),
        store=store,
        compressors=None,
        dispatch_policy=dispatch_policy,
        budget=MagicMock(),
        thread_id="thr_parent",
        submission_id="sub_123",
        emit=_emit,
        cancel=CancellationToken(name="test"),
        hooks=None,
        permission_policy=permission_policy,
        max_iterations=4,
    )
    # 父 stack
    runner.call_stack = runner.call_stack.push(skill_id="entry", call_id="entry_1")
    return runner


# --------------------------------------------------------------------
# Scenario: inherit 模式 → 直接透传父 policy + 无 event
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inherit_mode_uses_parent_policy_directly(
    captured_sub_policies: list[Any],
) -> None:
    policy = PermissionPolicy(rules=[], default_mode="allow")
    emitted: list[EventMsg] = []
    parent = _make_parent_runner(
        dispatch_policy=DispatchPolicy(subagent_approval_mode="inherit"),
        permission_policy=policy,
        emitted=emitted,
    )
    sub = _make_skill("sub")
    ctx = ToolContext(
        call_id="tc1",
        cancel=parent.cancel,
        thread_id=parent.thread_id,
        extras={},
    )
    await parent.run_sub_skill(
        target=sub,
        arguments={"x": 1},
        parent_stack=parent.call_stack,
        ctx=ctx,
    )
    assert captured_sub_policies == [policy]
    kinds = [ev.msg.kind for ev in emitted]
    assert "subagent_policy_overridden" not in kinds


# --------------------------------------------------------------------
# Scenario: auto_deny → 包装 + 1 个 event
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_deny_mode_wraps_parent_policy(
    captured_sub_policies: list[Any],
) -> None:
    policy = PermissionPolicy(rules=[], default_mode="allow")
    emitted: list[EventMsg] = []
    parent = _make_parent_runner(
        dispatch_policy=DispatchPolicy(subagent_approval_mode="auto_deny"),
        permission_policy=policy,
        emitted=emitted,
    )
    sub = _make_skill("sub")
    ctx = ToolContext(
        call_id="tc1",
        cancel=parent.cancel,
        thread_id=parent.thread_id,
        extras={},
    )
    await parent.run_sub_skill(
        target=sub,
        arguments={"x": 1},
        parent_stack=parent.call_stack,
        ctx=ctx,
    )
    assert len(captured_sub_policies) == 1
    sub_pol = captured_sub_policies[0]
    assert isinstance(sub_pol, _SubagentAutoDecisionPolicy)
    assert sub_pol.inner is policy
    assert sub_pol.fallback == "deny"
    # event emitted 一次，含正确字段
    overrides = [
        ev for ev in emitted if ev.msg.kind == "subagent_policy_overridden"
    ]
    assert len(overrides) == 1
    data = overrides[0].msg.data
    assert data["target_skill_id"] == "sub"
    assert data["mode"] == "auto_deny"
    assert data["depth"] == parent.call_stack.depth + 1


# --------------------------------------------------------------------
# Scenario: auto_allow → 包装 fallback=allow + event mode=auto_allow
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_allow_mode_wraps_parent_policy(
    captured_sub_policies: list[Any],
) -> None:
    policy = PermissionPolicy(rules=[], default_mode="allow")
    emitted: list[EventMsg] = []
    parent = _make_parent_runner(
        dispatch_policy=DispatchPolicy(subagent_approval_mode="auto_allow"),
        permission_policy=policy,
        emitted=emitted,
    )
    sub = _make_skill("sub")
    ctx = ToolContext(
        call_id="tc1", cancel=parent.cancel,
        thread_id=parent.thread_id, extras={},
    )
    await parent.run_sub_skill(
        target=sub, arguments={"x": 1},
        parent_stack=parent.call_stack, ctx=ctx,
    )
    sub_pol = captured_sub_policies[0]
    assert isinstance(sub_pol, _SubagentAutoDecisionPolicy)
    assert sub_pol.fallback == "allow"
    data = next(
        ev.msg.data for ev in emitted
        if ev.msg.kind == "subagent_policy_overridden"
    )
    assert data["mode"] == "auto_allow"


# --------------------------------------------------------------------
# Scenario: 父 policy=None + auto_deny → 不包装 + 不 emit
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_deny_with_none_parent_policy_no_wrap_no_emit(
    captured_sub_policies: list[Any],
) -> None:
    emitted: list[EventMsg] = []
    parent = _make_parent_runner(
        dispatch_policy=DispatchPolicy(subagent_approval_mode="auto_deny"),
        permission_policy=None,
        emitted=emitted,
    )
    sub = _make_skill("sub")
    ctx = ToolContext(
        call_id="tc1", cancel=parent.cancel,
        thread_id=parent.thread_id, extras={},
    )
    await parent.run_sub_skill(
        target=sub, arguments={"x": 1},
        parent_stack=parent.call_stack, ctx=ctx,
    )
    assert captured_sub_policies == [None]
    kinds = [ev.msg.kind for ev in emitted]
    assert "subagent_policy_overridden" not in kinds


# --------------------------------------------------------------------
# Scenario: inherit + 父 policy=None → 子也 None，无 event
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inherit_mode_with_none_parent_policy(
    captured_sub_policies: list[Any],
) -> None:
    emitted: list[EventMsg] = []
    parent = _make_parent_runner(
        dispatch_policy=DispatchPolicy(subagent_approval_mode="inherit"),
        permission_policy=None,
        emitted=emitted,
    )
    sub = _make_skill("sub")
    ctx = ToolContext(
        call_id="tc1", cancel=parent.cancel,
        thread_id=parent.thread_id, extras={},
    )
    await parent.run_sub_skill(
        target=sub, arguments={"x": 1},
        parent_stack=parent.call_stack, ctx=ctx,
    )
    assert captured_sub_policies == [None]
    kinds = [ev.msg.kind for ev in emitted]
    assert "subagent_policy_overridden" not in kinds
