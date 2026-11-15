"""``HookRegistry.unregister``：按对象身份撤销钩子（plugin-tool-hook-registration）。"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.hooks.types import HookContext, HookDecision, HookRegistry, HookRunner


def _ctx() -> HookContext:
    return HookContext(thread_id="t", submission_id="s", entry_skill_id="e")


async def test_unregistered_handler_no_longer_runs() -> None:
    calls: list[str] = []

    async def first(hook: Any, ctx: HookContext) -> HookDecision:
        calls.append("first")
        return HookDecision.ok()

    async def second(hook: Any, ctx: HookContext) -> HookDecision:
        calls.append("second")
        return HookDecision.ok()

    registry = HookRegistry()
    registry.register("pre_turn", first)
    registry.register("pre_turn", second)
    registry.unregister("pre_turn", first)
    await HookRunner(registry).run("pre_turn", object(), _ctx())
    assert calls == ["second"]


def test_unregister_unknown_handler_raises() -> None:
    async def handler(hook: Any, ctx: HookContext) -> HookDecision:
        return HookDecision.ok()

    registry = HookRegistry()
    with pytest.raises(ValueError):
        registry.unregister("pre_turn", handler)
    registry.register("post_turn", handler)
    with pytest.raises(ValueError):  # 注册在别的 kind 下也不算
        registry.unregister("pre_turn", handler)


def test_unregister_removes_only_one_instance() -> None:
    async def handler(hook: Any, ctx: HookContext) -> HookDecision:
        return HookDecision.ok()

    registry = HookRegistry()
    registry.register("pre_turn", handler)
    registry.register("pre_turn", handler)
    registry.unregister("pre_turn", handler)
    assert registry.handlers("pre_turn") == [handler]


async def test_unregister_during_run_does_not_affect_current_round() -> None:
    calls: list[str] = []
    registry = HookRegistry()

    async def remover(hook: Any, ctx: HookContext) -> HookDecision:
        calls.append("remover")
        registry.unregister("pre_turn", later)
        return HookDecision.ok()

    async def later(hook: Any, ctx: HookContext) -> HookDecision:
        calls.append("later")
        return HookDecision.ok()

    registry.register("pre_turn", remover)
    registry.register("pre_turn", later)
    await HookRunner(registry).run("pre_turn", object(), _ctx())
    assert calls == ["remover", "later"]
    assert registry.handlers("pre_turn") == [remover]
