"""``ctx.tools`` / ``ctx.hooks`` 门面测试（plugin-tool-hook-registration）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from taifeng.hooks.types import HookContext, HookDecision, HookRegistry, HookRunner
from taifeng.loop.pool_plugins import _capabilities
from taifeng.plugin import (
    PluginCapabilities,
    PluginCapabilityError,
    PluginContext,
    PluginManager,
    PluginSpec,
)
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolResult, ToolSpec


def _tool(name: str) -> ToolSpec:
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, handler=handler)


async def _hook(hook: Any, ctx: HookContext) -> HookDecision:
    return HookDecision.ok()


class _Registers:
    """在 setup 里调用一个回调，便于各测试定制注册动作。"""

    def __init__(self, plugin_id: str, action: Any) -> None:
        self.spec = PluginSpec(id=plugin_id, version="1")
        self._action = action

    async def setup(self, ctx: PluginContext) -> None:
        self._action(ctx)


async def test_tool_registered_then_revoked_on_close() -> None:
    registry = ToolRegistry()
    manager = PluginManager(capabilities=PluginCapabilities(tools=registry))
    await manager.load([_Registers("p", lambda ctx: ctx.tools.register(_tool("echo")))])
    assert registry.get("echo") is not None
    await manager.close()
    assert registry.get("echo") is None


async def test_tool_name_conflict_rejected_and_rolls_back() -> None:
    registry = ToolRegistry([_tool("echo")])
    original = registry.get("echo")
    manager = PluginManager(capabilities=PluginCapabilities(tools=registry))
    with pytest.raises(Exception) as caught:
        await manager.load([_Registers("p", lambda ctx: ctx.tools.register(_tool("echo")))])
    assert isinstance(caught.value.__cause__, PluginCapabilityError)
    assert registry.get("echo") is original  # 已有工具不被覆盖


async def test_conflict_between_two_plugins() -> None:
    registry = ToolRegistry()
    manager = PluginManager(capabilities=PluginCapabilities(tools=registry))
    with pytest.raises(Exception) as caught:
        await manager.load([
            _Registers("a", lambda ctx: ctx.tools.register(_tool("echo"))),
            _Registers("b", lambda ctx: ctx.tools.register(_tool("echo"))),
        ])
    assert isinstance(caught.value.__cause__, PluginCapabilityError)
    assert registry.get("echo") is None  # a 已随回滚撤销


async def test_revoke_does_not_remove_a_tool_someone_else_replaced() -> None:
    registry = ToolRegistry()
    manager = PluginManager(capabilities=PluginCapabilities(tools=registry))
    await manager.load([_Registers("p", lambda ctx: ctx.tools.register(_tool("echo")))])
    replacement = _tool("echo")
    registry.replace(replacement)
    with pytest.raises(Exception):  # 撤销失败计为关闭错误
        await manager.close()
    assert registry.get("echo") is replacement


async def test_missing_capability_is_explicit() -> None:
    manager = PluginManager()  # 不提供任何 capability
    with pytest.raises(Exception) as caught:
        await manager.load([_Registers("p", lambda ctx: ctx.tools.register(_tool("echo")))])
    cause = caught.value.__cause__
    assert isinstance(cause, PluginCapabilityError)
    assert cause.capability == "tools"


async def test_hook_registered_then_revoked() -> None:
    registry = HookRegistry()
    manager = PluginManager(capabilities=PluginCapabilities(hooks=lambda: registry))
    await manager.load([_Registers("p", lambda ctx: ctx.hooks.register("pre_turn", _hook))])
    assert registry.handlers("pre_turn") == [_hook]
    await manager.close()
    assert registry.handlers("pre_turn") == []


async def test_hooks_run_in_plugin_load_order() -> None:
    registry = HookRegistry()
    order: list[str] = []

    def make(name: str) -> Any:
        async def handler(hook: Any, ctx: HookContext) -> HookDecision:
            order.append(name)
            return HookDecision.ok()

        return handler

    a, b = make("a"), make("b")
    manager = PluginManager(capabilities=PluginCapabilities(hooks=lambda: registry))
    await manager.load([
        _Registers("b", lambda ctx: ctx.hooks.register("pre_turn", b)),
        _Registers("a", lambda ctx: ctx.hooks.register("pre_turn", a)),
    ])
    await HookRunner(registry).run("pre_turn", object(), HookContext("t", "s", "e"))
    assert order == ["b", "a"]
    await manager.close()


def _fake_pool(**overrides: Any) -> Any:
    base = {
        "_audit": None, "_hooks": None, "_tool_registry": ToolRegistry(), "_engines": {},
        "_registry": object(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_pool_creates_hook_runner_only_on_demand() -> None:
    pool = _fake_pool()
    caps = _capabilities(pool)
    assert pool._hooks is None  # noqa: SLF001
    assert caps.hooks is not None
    registry = caps.hooks()
    assert isinstance(pool._hooks, HookRunner)  # noqa: SLF001
    assert registry is pool._hooks.registry  # noqa: SLF001


def test_pool_reuses_host_hook_runner() -> None:
    runner = HookRunner()
    caps = _capabilities(_fake_pool(_hooks=runner))
    assert caps.hooks is not None
    assert caps.hooks() is runner.registry


def test_custom_hooks_object_denies_hook_capability() -> None:
    caps = _capabilities(_fake_pool(_hooks=object()))
    assert caps.hooks is not None
    assert caps.hooks() is None
    assert "HookRunner" in caps.unavailable["hooks"]


def test_audit_pool_denies_tools_hooks_and_skills() -> None:
    caps = _capabilities(_fake_pool(_audit=object()))
    assert caps.tools is None
    assert caps.hooks is None
    assert caps.skills is None
    assert set(caps.unavailable) == {"tools", "hooks", "skills"}


def test_custom_skill_registry_denies_skill_capability() -> None:
    caps = _capabilities(_fake_pool())
    assert caps.skills is None
    assert "FilesystemSkillRegistry" in caps.unavailable["skills"]
