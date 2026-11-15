"""插件 capability 的后端声明与门面（plugin-tool-hook-registration 起逐期增加）。

``PluginCapabilities`` 由 pool 接线层构造，描述本 pool 能给插件提供哪些 capability；
``PluginContext`` 把它包成 ``ctx.tools`` / ``ctx.hooks`` 等门面。门面只做三件事：
封口检查、调用既有注册表、把可撤销 handle 登记给 manager。不复制任何注册表。

参照 MCP bridge（``mcp/bridge.py``）在共享 ``ToolRegistry`` 上运行期增删工具的做法；
差异：插件不能 ``replace``，重名一律报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from taifeng.plugin.errors import PluginCapabilityError
from taifeng.tool.registry import DuplicateToolError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from taifeng.hooks.types import HookHandler, HookKind, HookRegistry
    from taifeng.plugin.context import PluginContext
    from taifeng.plugin.types import RegistrationHandle
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.spec import ToolSpec


@dataclass(frozen=True, slots=True)
class PluginCapabilities:
    """本 pool 可提供给插件的 capability 后端。

    Attributes:
        tools: 共享工具注册表；None 表示不可用。
        hooks: 取钩子注册表的回调（可按需创建 runner）；None 表示不可用，回调返回 None 表示
            宿主注入的 ``hooks=`` 不是可注册的 ``HookRunner``。
        unavailable: capability 名 → 不可用原因，进错误信息（如审计 pool 的静态门已封）。
    """

    tools: ToolRegistry | None = None
    hooks: Callable[[], HookRegistry | None] | None = None
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def denied(self, plugin_id: str, capability: str) -> PluginCapabilityError:
        """构造「capability 不可用」错误。"""
        reason = self.unavailable.get(capability, "not provided by this pool")
        return PluginCapabilityError(
            f"capability {capability!r} unavailable for plugin {plugin_id!r}: {reason}",
            plugin_id=plugin_id,
            capability=capability,
        )


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------


class _ToolHandle:
    """撤销一个插件注册的工具；只撤销仍是本插件注册的那个对象。"""

    __slots__ = ("_closed", "_registry", "_spec")

    def __init__(self, registry: ToolRegistry, spec: ToolSpec) -> None:
        self._registry = registry
        self._spec = spec
        self._closed = False

    async def aclose(self) -> None:
        """从注册表移除；已被他人替换时报错而不是误删别人的工具。"""
        if self._closed:
            return
        self._closed = True
        current = self._registry.get(self._spec.name)
        if current is not self._spec:
            raise RuntimeError(f"tool {self._spec.name!r} was replaced or removed by another owner")
        self._registry.unregister(self._spec.name)


class ToolCapability:
    """``ctx.tools``：向 pool 共享工具注册表注册可撤销的工具。"""

    __slots__ = ("_caps", "_ctx")

    def __init__(self, ctx: PluginContext, caps: PluginCapabilities) -> None:
        """由 ``PluginContext`` 构造。"""
        self._ctx = ctx
        self._caps = caps

    def register(self, spec: ToolSpec) -> RegistrationHandle:
        """注册工具；pool 关闭时自动撤销。

        Raises:
            PluginCapabilityError: context 已封口、本 pool 不提供工具注册，或工具重名。
        """
        self._ctx._ensure_open("tools")  # noqa: SLF001
        registry = self._caps.tools
        if registry is None:
            raise self._caps.denied(self._ctx.plugin_id, "tools")
        try:
            registry.register(spec)
        except DuplicateToolError as exc:
            raise PluginCapabilityError(
                f"plugin {self._ctx.plugin_id!r} tool {spec.name!r} conflicts with an existing tool",
                plugin_id=self._ctx.plugin_id,
                capability="tools",
            ) from exc
        handle = _ToolHandle(registry, spec)
        self._ctx._track(handle)  # noqa: SLF001
        return handle


# ----------------------------------------------------------------------
# hooks
# ----------------------------------------------------------------------


class _HookHandle:
    """撤销一个插件注册的钩子（按对象身份）。"""

    __slots__ = ("_closed", "_handler", "_kind", "_registry")

    def __init__(self, registry: HookRegistry, kind: HookKind, handler: HookHandler) -> None:
        self._registry = registry
        self._kind = kind
        self._handler = handler
        self._closed = False

    async def aclose(self) -> None:
        """从钩子注册表移除。"""
        if self._closed:
            return
        self._closed = True
        self._registry.unregister(self._kind, self._handler)


class HookCapability:
    """``ctx.hooks``：向 pool 的 ``HookRunner`` 注册可撤销的钩子。"""

    __slots__ = ("_caps", "_ctx")

    def __init__(self, ctx: PluginContext, caps: PluginCapabilities) -> None:
        """由 ``PluginContext`` 构造。"""
        self._ctx = ctx
        self._caps = caps

    def register(self, kind: HookKind, handler: HookHandler) -> RegistrationHandle:
        """注册钩子；多个插件的同类钩子按加载顺序串行执行。

        Raises:
            PluginCapabilityError: context 已封口，或本 pool 不提供钩子注册。
        """
        self._ctx._ensure_open("hooks")  # noqa: SLF001
        resolve = self._caps.hooks
        if resolve is None:
            raise self._caps.denied(self._ctx.plugin_id, "hooks")
        registry = resolve()
        if registry is None:
            raise self._caps.denied(self._ctx.plugin_id, "hooks")
        registry.register(kind, handler)
        handle = _HookHandle(registry, kind, handler)
        self._ctx._track(handle)  # noqa: SLF001
        return handle


__all__ = ["HookCapability", "PluginCapabilities", "ToolCapability"]
