"""DispatchPolicy + CallStack 测试。"""

from __future__ import annotations

from pathlib import Path

from taifeng.skill import CallStack, DispatchPolicy, SkillDefinition


def _mk(id_: str, child: frozenset[str] = frozenset(), entry: bool = False, max_depth: int = 6) -> SkillDefinition:
    return SkillDefinition(
        id=id_, name=id_, description="x", version="1", body="",
        body_path=Path("/tmp"), type="composite" if child or entry else "atomic",
        entry=entry, child_skills=child, max_call_depth=max_depth,
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
    """不能调用另一个 entry skill。"""
    caller = _mk("p", child=frozenset({"c"}), entry=True)
    target = _mk("c", entry=True, child=frozenset({"x"}))
    stack = CallStack().push("p", "call_p")
    v = DispatchPolicy().check(stack=stack, caller=caller, target=target)
    assert not v.allowed
    assert v.reason == "cannot_call_entry_skill"


def test_call_stack_path() -> None:
    s = CallStack().push("a", "1").push("b", "2").push("c", "3")
    assert s.depth == 3
    assert s.path() == ["a", "b", "c"]
    assert s.contains("b")
    assert not s.contains("z")
