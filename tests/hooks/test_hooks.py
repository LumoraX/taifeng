"""Hooks 系统测试。

覆盖：
    - 既有 PreToolUse / PostToolUse 钩子链路（原 3 用例）
    - T3: HookKind 扩展 pre_skill_dispatch / post_skill_dispatch
    - T3: PreSkillDispatchHook / PostSkillDispatchHook 数据类
    - T3: HookRunner.run_audit_only 不影响调用方
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from taifeng.hooks import (
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    PostSkillDispatchHook,
    PostToolUseHook,
    PreSkillDispatchHook,
    PreToolUseHook,
)


def _ctx() -> HookContext:
    return HookContext(thread_id="t1", submission_id="s1", entry_skill_id="x")


@pytest.mark.asyncio
async def test_no_hooks_allows() -> None:
    runner = HookRunner()
    d = await runner.run("pre_tool_use", PreToolUseHook(
        tool_name="x", arguments={}, parallel_safe=True, call_id="c1",
    ), _ctx())
    assert d.allow


@pytest.mark.asyncio
async def test_handler_allow_then_deny_stops() -> None:
    reg = HookRegistry()
    calls: list[str] = []

    async def h1(hook, ctx) -> HookDecision:
        calls.append("h1")
        return HookDecision.ok()

    async def h2(hook, ctx) -> HookDecision:
        calls.append("h2")
        return HookDecision.deny("nope")

    async def h3(hook, ctx) -> HookDecision:
        calls.append("h3")
        return HookDecision.ok()

    reg.register("pre_tool_use", h1)
    reg.register("pre_tool_use", h2)
    reg.register("pre_tool_use", h3)
    runner = HookRunner(reg)

    d = await runner.run("pre_tool_use", PreToolUseHook(
        tool_name="x", arguments={}, parallel_safe=True, call_id="c1",
    ), _ctx())
    assert not d.allow
    assert d.reason == "nope"
    assert calls == ["h1", "h2"]  # h3 没跑


@pytest.mark.asyncio
async def test_handler_exception_denies() -> None:
    reg = HookRegistry()

    async def bad(hook, ctx) -> HookDecision:
        raise RuntimeError("kaboom")

    reg.register("post_tool_use", bad)
    runner = HookRunner(reg)
    d = await runner.run("post_tool_use", PostToolUseHook(
        tool_name="x", arguments={}, output="", is_error=False, duration_ms=10, call_id="c1",
    ), _ctx())
    assert not d.allow
    assert "hook_error" in (d.reason or "")


# ====================================================================
# T3: pre_skill_dispatch / post_skill_dispatch
# ====================================================================


def test_register_skill_dispatch_hooks() -> None:
    """注册新 hook kind 后 handlers 返回非空。"""
    reg = HookRegistry()

    async def h(hook, ctx) -> HookDecision:
        return HookDecision.ok()

    reg.register("pre_skill_dispatch", h)
    reg.register("post_skill_dispatch", h)
    assert len(reg.handlers("pre_skill_dispatch")) == 1
    assert len(reg.handlers("post_skill_dispatch")) == 1
    # 既有 hook kind 仍为空（不退化）
    assert len(reg.handlers("pre_tool_use")) == 0


def test_pre_skill_dispatch_hook_is_frozen() -> None:
    """PreSkillDispatchHook 是 frozen dataclass。"""
    hook = PreSkillDispatchHook(
        target_skill_id="D",
        args={"x": 1},
        caller_skill_id="C",
        call_chain=("A", "B", "C"),
        depth=4,
    )
    with pytest.raises(FrozenInstanceError):
        hook.target_skill_id = "X"  # type: ignore[misc]


def test_post_skill_dispatch_hook_is_frozen() -> None:
    """PostSkillDispatchHook 是 frozen dataclass。"""
    hook = PostSkillDispatchHook(
        target_skill_id="D",
        caller_skill_id="C",
        success=True,
        duration_ms=123,
        sub_thread_id="t-sub",
        output_preview="abc",
    )
    with pytest.raises(FrozenInstanceError):
        hook.success = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_run_audit_only_swallows_deny() -> None:
    """run_audit_only：hook 返回 deny 时不影响调用方，只走完全部。"""
    reg = HookRegistry()
    calls: list[str] = []

    async def h1(hook, ctx) -> HookDecision:
        calls.append("h1")
        return HookDecision.deny("nope")

    async def h2(hook, ctx) -> HookDecision:
        calls.append("h2")
        return HookDecision.ok()

    reg.register("post_skill_dispatch", h1)
    reg.register("post_skill_dispatch", h2)
    runner = HookRunner(reg)
    # 返回 None；两个 handler 都执行
    result = await runner.run_audit_only(
        "post_skill_dispatch",
        PostSkillDispatchHook(
            target_skill_id="D",
            caller_skill_id="C",
            success=True,
            duration_ms=10,
            sub_thread_id=None,
            output_preview="",
        ),
        _ctx(),
    )
    assert result is None
    assert calls == ["h1", "h2"]


@pytest.mark.asyncio
async def test_run_audit_only_swallows_exception() -> None:
    """run_audit_only：hook 抛异常不影响调用方，仅记日志。"""
    reg = HookRegistry()
    calls: list[str] = []

    async def bad(hook, ctx) -> HookDecision:
        calls.append("bad")
        raise RuntimeError("kaboom")

    async def ok(hook, ctx) -> HookDecision:
        calls.append("ok")
        return HookDecision.ok()

    reg.register("post_skill_dispatch", bad)
    reg.register("post_skill_dispatch", ok)
    runner = HookRunner(reg)
    # 应正常返回 None；ok handler 仍执行
    result = await runner.run_audit_only(
        "post_skill_dispatch",
        PostSkillDispatchHook(
            target_skill_id="D",
            caller_skill_id="C",
            success=False,
            duration_ms=10,
            sub_thread_id=None,
            output_preview="",
        ),
        _ctx(),
    )
    assert result is None
    assert calls == ["bad", "ok"]
