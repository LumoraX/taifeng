"""DispatchPolicy + CallStack 测试。"""

from __future__ import annotations

from pathlib import Path

from taifeng.skill import CallStack, DispatchPolicy, SkillDefinition


def _mk(
    id_: str,
    child: frozenset[str] = frozenset(),
    entry: bool = False,
    max_depth: int = 6,
    tools: frozenset[str] = frozenset(),
) -> SkillDefinition:
    return SkillDefinition(
        id=id_, name=id_, description="x", version="1", body="",
        body_path=Path("/tmp"),
        type="composite" if (child or entry or tools) else "atomic",
        entry=entry, child_skills=child, tool_names=tools, max_call_depth=max_depth,
    )


def test_dispatch_allow_in_whitelist() -> None:
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    target = _mk("c")
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert v.allowed


def test_dispatch_reject_not_in_whitelist() -> None:
    caller = _mk("p", child=frozenset({"x"}), entry=True)
    target = _mk("c")
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "not_in_whitelist"


def test_dispatch_reject_cycle() -> None:
    caller = _mk("p", child=frozenset({"a"}), entry=True)
    target = _mk("p")  # 调回自己
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "cycle_detected"


def test_dispatch_reject_max_depth() -> None:
    caller = _mk("p", child=frozenset({"c"}), entry=True, max_depth=2)
    target = _mk("c")
    stack = CallStack().push("p", "1").push("a", "2")  # depth=2
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "max_depth_exceeded"


def test_dispatch_reject_unknown_target() -> None:
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=None)
    assert not v.allowed
    assert v.reason == "unknown_skill"


def test_dispatch_reject_entry_target() -> None:
    """call_skill（嵌套子调用）不能调用另一个 entry skill（默认 allow_entry_target=False）。"""
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    target = _mk("c", entry=True, child=frozenset({"x"}))
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "cannot_call_entry_skill"


def test_dispatch_allow_entry_target_for_spawn() -> None:
    """detached spawn（allow_entry_target=True）可以分离发起一个 entry skill。

    spawn 把目标作为独立 child thread（等价于另起一个根），调 entry skill 是正当用法；
    其余四层（存在 / 深度 / 环 / 白名单）仍照常裁决。
    """
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    target = _mk("c", entry=True, child=frozenset({"x"}))
    stack = CallStack().push("p", "spawn_p")
    v = DispatchPolicy().check(
        stack=stack, caller=caller, target=target, allow_entry_target=True,
    )
    assert v.allowed


def test_dispatch_allow_entry_target_still_enforces_whitelist() -> None:
    """allow_entry_target 只跳过 entry 门——白名单等其余裁决仍生效。"""
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    target = _mk("other", entry=True)  # 不在 caller.child_skills 白名单
    stack = CallStack().push("p", "spawn_p")
    v = DispatchPolicy().check(
        stack=stack, caller=caller, target=target, allow_entry_target=True,
    )
    assert not v.allowed
    assert v.reason == "not_in_whitelist"


def test_dispatch_reject_call_skill_from_tool_only_composite() -> None:
    """tool-only composite（空 child_skills、仅 tool_names）发起 call_skill 必被拒：
    白名单为空 → 任何 target 都不在其中（ADR 0013：tool-only composite 无派发能力）。"""
    caller = _mk("leaf", tools=frozenset({"request_user_input"}))
    target = _mk("c")
    stack = CallStack().push("leaf", "call_leaf")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "not_in_whitelist"


def test_call_stack_path() -> None:
    s = CallStack().push("a", "1").push("b", "2").push("c", "3")
    assert s.depth == 3
    assert s.path() == ["a", "b", "c"]
    assert s.contains("b")
    assert not s.contains("z")
