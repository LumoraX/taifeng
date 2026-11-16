"""taifeng.plugin —— 内核插件系统（🧪 实验层，ADR 0144）。

宿主把扩展写成插件，显式交给 ``EnginePool.create(plugins=...)``；内核负责依赖顺序、
生命周期、回滚、取消与可观测。对外入口经 ``taifeng.experimental`` 导出。
"""

from __future__ import annotations

from taifeng.plugin.context import PluginContext
from taifeng.plugin.errors import (
    DuplicatePluginError,
    MissingPluginDependencyError,
    PluginCapabilityError,
    PluginCloseError,
    PluginDependencyCycleError,
    PluginError,
    PluginSetupError,
    PluginStateError,
)
from taifeng.plugin.manager import PluginManager
from taifeng.plugin.types import (
    Plugin,
    PluginLifecycleEvent,
    PluginSnapshot,
    PluginSpec,
    RegistrationHandle,
)

__all__ = [
    "DuplicatePluginError",
    "MissingPluginDependencyError",
    "Plugin",
    "PluginCapabilityError",
    "PluginCloseError",
    "PluginContext",
    "PluginDependencyCycleError",
    "PluginError",
    "PluginLifecycleEvent",
    "PluginManager",
    "PluginSetupError",
    "PluginSnapshot",
    "PluginSpec",
    "PluginStateError",
    "RegistrationHandle",
]
