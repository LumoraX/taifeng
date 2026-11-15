"""PluginContext —— 插件能接触内核的唯一入口（capability allowlist，ADR 0144）。

插件只能调用本类上显式列出的成员；拿不到 engine、runner、prompt、history 或业务对象。
各 capability 门面（``tools`` / ``hooks`` …，见 ``plugin/capabilities.py``）通过
``_ensure_open`` + ``_track`` 把注册登记给 manager，由 manager 统一撤销。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.plugin.capabilities import HookCapability, PluginCapabilities, ToolCapability
from taifeng.plugin.errors import PluginCapabilityError

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.plugin.types import RegistrationHandle


class PluginContext:
    """单个插件 setup 期间可用的受限上下文。

    setup 返回后 context 封口，之后的任何注册都抛 ``PluginCapabilityError``——
    防止插件把 context 存下来，在 manager 不知情的时机注册东西。
    """

    __slots__ = ("_cancel", "_caps", "_plugin_id", "_sealed", "_track_fn")

    def __init__(
        self,
        *,
        plugin_id: str,
        cancel: CancellationToken,
        track: Callable[[RegistrationHandle], None],
        capabilities: PluginCapabilities | None = None,
    ) -> None:
        """构造 context（由 manager 调用，插件不应自己构造）。

        Args:
            plugin_id: 所属插件 id。
            cancel: 由 pool 根令牌派生的取消令牌。
            track: 把注册 handle 登记给 manager 的回调。
            capabilities: 本 pool 提供的 capability 后端；None 表示一个都不提供。
        """
        self._plugin_id = plugin_id
        self._cancel = cancel
        self._track_fn = track
        self._sealed = False
        self._caps = capabilities or PluginCapabilities()

    @property
    def plugin_id(self) -> str:
        """所属插件 id。"""
        return self._plugin_id

    @property
    def cancel(self) -> CancellationToken:
        """由 pool 根令牌派生的取消令牌（R4）；pool 关闭时被取消。"""
        return self._cancel

    @property
    def tools(self) -> ToolCapability:
        """向 pool 共享工具注册表注册可撤销的工具。"""
        return ToolCapability(self, self._caps)

    @property
    def hooks(self) -> HookCapability:
        """向 pool 的钩子 runner 注册可撤销的钩子。"""
        return HookCapability(self, self._caps)

    def _ensure_open(self, capability: str) -> None:
        """capability 实现在真正注册之前调用：context 已封口则拒绝。

        Args:
            capability: capability 名，进错误信息。

        Raises:
            PluginCapabilityError: context 已封口。
        """
        if self._sealed:
            raise PluginCapabilityError(
                f"plugin {self._plugin_id!r} registered {capability!r} after setup returned",
                plugin_id=self._plugin_id,
                capability=capability,
            )

    def _track(self, handle: RegistrationHandle) -> None:
        """把一次已完成的注册登记给 manager，由 manager 在关闭 / 回滚时撤销。"""
        self._track_fn(handle)

    def _seal(self) -> None:
        """封口：setup 结束（成功或失败）后由 manager 调用。"""
        self._sealed = True


__all__ = ["PluginContext"]
