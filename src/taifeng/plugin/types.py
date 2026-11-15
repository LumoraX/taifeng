"""插件系统的数据类型与协议（ADR 0144）。

参照 Cordis / VS Code 扩展宿主的「声明身份 + 依赖 + 激活」范式；差异：只接受宿主显式注入，
没有全局插件图与自动发现，作用域第一版只有 ``pool``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from taifeng.plugin.context import PluginContext

PluginScope = Literal["pool"]
"""插件作用域。第一版只有 ``pool``：随 EnginePool 装配、随 pool 关闭拆除。"""

PluginManagerState = Literal["new", "loading", "loaded", "closing", "closed", "closed_with_errors"]
"""manager 状态机：``new → loading → loaded → closing → closed | closed_with_errors``。"""

PluginEventKind = Literal["plugin_loaded", "plugin_failed", "plugin_unloaded"]
"""插件生命周期事件的 kind。"""


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """插件的身份声明。

    Attributes:
        id: pool 内唯一的插件 id。
        version: 插件版本，只用于事件与诊断。
        requires: 依赖的插件 id；manager 保证依赖先于本插件 setup、晚于本插件关闭。
        scope: 作用域，第一版只能是 ``pool``。
    """

    id: str
    version: str
    requires: tuple[str, ...] = ()
    scope: PluginScope = "pool"

    def __post_init__(self) -> None:
        """校验字段；非法声明在构造时就失败，而不是拖到加载时。"""
        if not self.id:
            raise ValueError("PluginSpec.id must be non-empty")
        if not self.version:
            raise ValueError(f"PluginSpec.version must be non-empty (plugin {self.id!r})")
        if self.scope != "pool":
            raise ValueError(f"unsupported plugin scope {self.scope!r} (plugin {self.id!r})")
        if not isinstance(self.requires, tuple):
            raise TypeError(f"PluginSpec.requires must be a tuple (plugin {self.id!r})")


@runtime_checkable
class RegistrationHandle(Protocol):
    """一次可撤销的注册。``aclose`` 必须幂等。"""

    async def aclose(self) -> None:
        """撤销注册；重复调用无副作用。"""
        ...


@runtime_checkable
class Plugin(Protocol):
    """插件协议。

    ``setup`` 不返回值：插件经 context 做的注册由 context 自动登记，manager 负责逆序撤销。
    插件可选地实现 ``async close()``，它在该插件的注册被撤销之前调用；还可选实现同步的
    ``provide_backends(backends)``，在 pool 构造存储之前声明后端工厂（ADR 0145）。
    """

    @property
    def spec(self) -> PluginSpec:
        """插件身份声明。"""
        ...

    async def setup(self, ctx: PluginContext) -> None:
        """装配插件；抛错会让整组插件回滚、pool 创建失败。"""
        ...


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    """manager 的只读状态快照。

    Attributes:
        state: 当前状态。
        loaded_ids: 已加载插件的 id，按加载顺序。
    """

    state: PluginManagerState
    loaded_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginLifecycleEvent:
    """manager 发出的生命周期事件（与 EventMsg 解耦，由 pool 接线层转换）。

    Attributes:
        kind: 事件类型。
        data: 事件字段，只含 id / 版本 / 作用域 / 耗时 / 失败类别等元数据。
    """

    kind: PluginEventKind
    data: Mapping[str, object] = field(default_factory=dict)


PluginEventCallback = Callable[[PluginLifecycleEvent], Awaitable[None]]
"""生命周期事件回调；抛错只记日志，不影响插件生命周期。"""


__all__ = [
    "Plugin",
    "PluginEventCallback",
    "PluginEventKind",
    "PluginLifecycleEvent",
    "PluginManagerState",
    "PluginScope",
    "PluginSnapshot",
    "PluginSpec",
    "RegistrationHandle",
]
