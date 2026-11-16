"""插件系统的错误集合（ADR 0144）。

每个错误带稳定的 ``code``，消息只含插件 id、capability 名与 code，不含插件配置正文——
错误会进日志与 ``plugin_failed`` 事件，不能借此外泄业务数据。
"""

from __future__ import annotations

from typing import ClassVar


class PluginError(Exception):
    """插件系统错误基类。

    Attributes:
        code: 稳定错误码，供宿主按类别处理。
        plugin_id: 出错插件的 id；与具体插件无关时为 None。
    """

    code: ClassVar[str] = "plugin_error"

    def __init__(self, message: str, *, plugin_id: str | None = None) -> None:
        """构造错误。

        Args:
            message: 不含配置正文的简短说明。
            plugin_id: 出错插件的 id。
        """
        super().__init__(f"[{self.code}] {message}")
        self.plugin_id = plugin_id


class DuplicatePluginError(PluginError):
    """两个插件的 id 相同。"""

    code: ClassVar[str] = "duplicate_plugin"


class MissingPluginDependencyError(PluginError):
    """``requires`` 指向一个未传入的插件。"""

    code: ClassVar[str] = "missing_dependency"


class PluginDependencyCycleError(PluginError):
    """依赖成环（含插件依赖自身）。"""

    code: ClassVar[str] = "dependency_cycle"


class PluginSetupError(PluginError):
    """某插件 setup 抛错或被取消；原始异常挂在 ``__cause__``。"""

    code: ClassVar[str] = "setup_failed"


class PluginCloseError(PluginError):
    """关闭插件时有插件出错或超时；其余插件与资源照常清理后才抛出。

    Attributes:
        error_count: 关闭过程中出错（含超时）的次数。
    """

    code: ClassVar[str] = "close_failed"

    def __init__(self, message: str, *, error_count: int) -> None:
        """构造错误。

        Args:
            message: 简短说明。
            error_count: 出错次数。
        """
        super().__init__(message)
        self.error_count = error_count


class PluginCapabilityError(PluginError):
    """插件请求的 capability 不可用、冲突，或 context 已封口。

    Attributes:
        capability: 被拒绝的 capability 名。
    """

    code: ClassVar[str] = "capability_denied"

    def __init__(self, message: str, *, plugin_id: str | None, capability: str) -> None:
        """构造错误。

        Args:
            message: 简短说明。
            plugin_id: 发起请求的插件 id。
            capability: 被拒绝的 capability 名。
        """
        super().__init__(message, plugin_id=plugin_id)
        self.capability = capability


class PluginStateError(PluginError):
    """在不允许的状态下调用 manager（例如重复 ``load``）。"""

    code: ClassVar[str] = "invalid_state"


__all__ = [
    "DuplicatePluginError",
    "MissingPluginDependencyError",
    "PluginCapabilityError",
    "PluginCloseError",
    "PluginDependencyCycleError",
    "PluginError",
    "PluginSetupError",
    "PluginStateError",
]
