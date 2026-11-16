"""PluginManager —— 插件的依赖排序、加载、回滚与有界关闭（ADR 0144）。

参照 Cordis 的插件生命周期（依赖先行、失败回滚、逆序释放）；差异：没有全局插件图、
不热重载，注册只在关闭或回滚时撤销，事件经回调交给 pool 接线层转为 EventMsg。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

from taifeng.loop.cancellation import CancellationToken, interrupt_on_cancel
from taifeng.plugin.context import PluginContext
from taifeng.plugin.errors import (
    DuplicatePluginError,
    MissingPluginDependencyError,
    PluginCloseError,
    PluginDependencyCycleError,
    PluginSetupError,
    PluginStateError,
)
from taifeng.plugin.types import (
    PluginEventKind,
    PluginLifecycleEvent,
    PluginManagerState,
    PluginSnapshot,
    PluginSpec,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.plugin.types import Plugin, PluginEventCallback, RegistrationHandle

logger = logging.getLogger(__name__)

DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass
class _Loaded:
    """一个已开始 setup 的插件及其注册。"""

    plugin: Plugin
    ctx: PluginContext
    handles: list[RegistrationHandle] = field(default_factory=list)


def resolve_load_order(plugins: Sequence[Plugin]) -> list[Plugin]:
    """校验依赖图并给出确定的加载顺序。

    每一步取「依赖已全部就位」的插件里传入顺序最靠前的一个（Kahn 排序，同层保持传入顺序）。

    Args:
        plugins: 宿主传入的插件。

    Returns:
        加载顺序。

    Raises:
        TypeError: 某个对象没有合法的 ``spec``。
        DuplicatePluginError / MissingPluginDependencyError / PluginDependencyCycleError。
    """
    by_id: dict[str, Plugin] = {}
    for plugin in plugins:
        spec = getattr(plugin, "spec", None)
        if not isinstance(spec, PluginSpec):
            raise TypeError(f"plugin object {type(plugin).__name__} has no PluginSpec 'spec'")
        if spec.id in by_id:
            raise DuplicatePluginError(f"duplicate plugin id {spec.id!r}", plugin_id=spec.id)
        by_id[spec.id] = plugin
    for plugin_id, plugin in by_id.items():
        for dep in plugin.spec.requires:
            if dep not in by_id:
                raise MissingPluginDependencyError(
                    f"plugin {plugin_id!r} requires missing plugin {dep!r}", plugin_id=plugin_id,
                )
    ordered: list[Plugin] = []
    placed: set[str] = set()
    pending = list(by_id.values())
    while pending:
        ready = next((p for p in pending if set(p.spec.requires) <= placed), None)
        if ready is None:
            ids = ", ".join(sorted(p.spec.id for p in pending))
            raise PluginDependencyCycleError(f"plugin dependency cycle among: {ids}")
        ordered.append(ready)
        placed.add(ready.spec.id)
        pending.remove(ready)
    return ordered


class PluginManager:
    """持有一组插件的生命周期。

    用法（通常由 ``EnginePool.create(plugins=...)`` 代劳）：
        manager = PluginManager(cancel=root_token, on_event=emit)
        await manager.load(plugins)
        ...
        await manager.close()
    """

    def __init__(
        self,
        *,
        cancel: CancellationToken | None = None,
        on_event: PluginEventCallback | None = None,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        """构造 manager。

        Args:
            cancel: 父取消令牌；每个插件的 ``ctx.cancel`` 由它派生。None 时自建根令牌。
            on_event: 生命周期事件回调；None 时只记日志。
            close_timeout_seconds: 关闭单个插件（及撤销其每个注册）的超时上限。
        """
        if close_timeout_seconds <= 0:
            raise ValueError(f"close_timeout_seconds must be positive, got {close_timeout_seconds}")
        self._cancel = cancel if cancel is not None else CancellationToken(name="plugins")
        self._on_event = on_event
        self._close_timeout = close_timeout_seconds
        self._state: PluginManagerState = "new"
        self._loaded: list[_Loaded] = []

    def snapshot(self) -> PluginSnapshot:
        """返回当前状态与已加载插件 id（按加载顺序）。"""
        return PluginSnapshot(
            state=self._state,
            loaded_ids=tuple(entry.plugin.spec.id for entry in self._loaded),
        )

    async def load(self, plugins: Sequence[Plugin]) -> None:
        """按依赖顺序 setup 全部插件；任一失败则回滚已加载的插件后抛错。

        Raises:
            PluginStateError: manager 不在 ``new`` 状态。
            PluginSetupError: 某插件 setup 抛错或被取消（原始异常为 ``__cause__``）。
            依赖图错误：见 ``resolve_load_order``，此时没有任何 setup 被调用。
        """
        if self._state != "new":
            raise PluginStateError(f"load() called in state {self._state!r}")
        ordered = resolve_load_order(plugins)
        self._state = "loading"
        for plugin in ordered:
            await self._setup_one(plugin)
        self._state = "loaded"

    async def close(self) -> None:
        """逆加载顺序关闭全部插件；重复调用无副作用。

        Raises:
            PluginCloseError: 有插件关闭出错或超时（其余插件仍全部关闭后才抛）。
        """
        if self._state in ("closed", "closed_with_errors", "closing"):
            return
        self._state = "closing"
        errors = await self._close_all()
        self._state = "closed_with_errors" if errors else "closed"
        if errors:
            raise PluginCloseError(f"{errors} error(s) while closing plugins", error_count=errors)

    # ------------------------------------------------------------------
    # setup / 回滚
    # ------------------------------------------------------------------

    async def _setup_one(self, plugin: Plugin) -> None:
        """setup 单个插件；失败时撤销它自己的注册并回滚此前的插件。"""
        spec = plugin.spec
        token = self._cancel.child(f"plugin:{spec.id}")
        handles: list[RegistrationHandle] = []
        ctx = PluginContext(plugin_id=spec.id, cancel=token, track=handles.append)
        entry = _Loaded(plugin=plugin, ctx=ctx, handles=handles)
        started = time.monotonic()
        try:
            token.raise_if_cancelled()
            async with interrupt_on_cancel(token):
                await plugin.setup(entry.ctx)
        except BaseException as exc:
            entry.ctx._seal()  # noqa: SLF001
            await self._fail(entry, exc, started)
        entry.ctx._seal()  # noqa: SLF001
        self._loaded.append(entry)
        await self._emit("plugin_loaded", spec, started)

    async def _fail(self, entry: _Loaded, exc: BaseException, started: float) -> NoReturn:
        """setup 失败：撤销失败插件的注册 → 逆序关闭已加载插件 → 发事件 → 抛错。"""
        spec = entry.plugin.spec
        rollback_errors = await self._revoke(entry)
        rollback_errors += await self._close_all()
        self._state = "closed_with_errors" if rollback_errors else "closed"
        await self._emit(
            "plugin_failed", spec, started,
            failure_class=type(exc).__name__, rollback_errors=rollback_errors,
        )
        # 外部取消（非本插件令牌引起）照常外抛，保持调用方的 task 取消语义
        if isinstance(exc, Exception) or entry.ctx.cancel.is_cancelled:
            raise PluginSetupError(
                f"plugin {spec.id!r} setup failed ({type(exc).__name__})", plugin_id=spec.id,
            ) from exc
        raise exc

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    async def _close_all(self) -> int:
        """逆加载顺序关闭全部已加载插件，返回出错次数。"""
        errors = 0
        while self._loaded:
            errors += await self._close_entry(self._loaded.pop())
        return errors

    async def _close_entry(self, entry: _Loaded) -> int:
        """先调插件可选的 ``close()``，再撤销它的注册；返回出错次数。"""
        started = time.monotonic()
        failure: str | None = None
        errors = 0
        closer = getattr(entry.plugin, "close", None)
        if closer is not None:
            try:
                async with asyncio.timeout(self._close_timeout):
                    await closer()
            except Exception as exc:  # noqa: BLE001 —— 记录后继续，保证其余清理
                logger.warning("plugin %r close failed: %s", entry.plugin.spec.id, type(exc).__name__)
                failure = type(exc).__name__
                errors += 1
        revoke_errors = await self._revoke(entry)
        if revoke_errors and failure is None:
            failure = "RegistrationRevokeError"
        await self._emit("plugin_unloaded", entry.plugin.spec, started, failure_class=failure)
        return errors + revoke_errors

    async def _revoke(self, entry: _Loaded) -> int:
        """逆注册顺序撤销插件的全部注册，返回出错次数。"""
        errors = 0
        while entry.handles:
            handle = entry.handles.pop()
            try:
                async with asyncio.timeout(self._close_timeout):
                    await handle.aclose()
            except Exception as exc:  # noqa: BLE001 —— 单个撤销失败不阻断其余撤销
                logger.warning(
                    "plugin %r registration revoke failed: %s",
                    entry.plugin.spec.id, type(exc).__name__,
                )
                errors += 1
        return errors

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    async def _emit(
        self,
        kind: PluginEventKind,
        spec: PluginSpec,
        started: float,
        **extra: object,
    ) -> None:
        """发生命周期事件；回调失败只记日志。"""
        data: dict[str, object] = {
            "plugin_id": spec.id,
            "version": spec.version,
            "scope": spec.scope,
            "duration_ms": int((time.monotonic() - started) * 1000),
            **extra,
        }
        event = PluginLifecycleEvent(kind=kind, data=data)
        if self._on_event is None:
            logger.info("%s %s", kind, data)
            return
        try:
            await self._on_event(event)
        except Exception as exc:  # noqa: BLE001 —— 观察者失败不影响生命周期
            logger.warning("plugin event callback failed for %s: %s", kind, type(exc).__name__)


__all__ = ["DEFAULT_CLOSE_TIMEOUT_SECONDS", "PluginManager", "resolve_load_order"]
