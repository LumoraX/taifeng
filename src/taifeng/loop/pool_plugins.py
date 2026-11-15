"""EnginePool 的插件接线（plugin-core，ADR 0144）。

从 ``loop/pool.py`` 拆出（该文件逼近 800 行红线）：pool 只透传 ``plugins=`` 并在两处调用本模块——
``create`` 构造出 pool 之后加载，``close`` 清理资源时最先关闭。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from taifeng.loop.event import EventMsg, PluginFailed, PluginLoaded, PluginUnloaded
from taifeng.plugin.manager import PluginManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.loop.pool import EnginePool
    from taifeng.plugin.types import Plugin, PluginEventCallback, PluginLifecycleEvent
    from taifeng.telemetry.sink import TelemetrySink

logger = logging.getLogger(__name__)

_EVENT_CLASSES = {
    "plugin_loaded": PluginLoaded,
    "plugin_failed": PluginFailed,
    "plugin_unloaded": PluginUnloaded,
}


def _sink_emitter(sink: TelemetrySink | None) -> PluginEventCallback | None:
    """把 manager 的生命周期事件转成 pool 级 EventMsg 交给 ``sink``。

    插件在首个 engine 之前加载，没有 engine 事件总线：沿用持久化层事件的先例，
    ``submission_id="*"``、``seq=0`` 直接交给 sink。无 sink 时返回 None（manager 只记日志）。
    """
    if sink is None:
        return None

    async def _emit(event: PluginLifecycleEvent) -> None:
        msg = _EVENT_CLASSES[event.kind](data=dict(event.data))
        await sink.handle(EventMsg(submission_id="*", msg=msg))

    return _emit


async def load_pool_plugins(
    pool: EnginePool,
    plugins: Sequence[Plugin],
    *,
    sink: TelemetrySink | None,
) -> None:
    """按依赖顺序加载插件；未传插件时什么都不做（默认路径不变）。

    Args:
        pool: 已构造、尚无 engine 的 pool。
        plugins: 宿主显式传入的插件。
        sink: pool 级 sink，接收插件生命周期事件。

    Raises:
        PluginSetupError 等：加载失败时已回滚；调用方负责关闭 pool。
    """
    if not plugins:
        return
    manager = PluginManager(cancel=pool._root_cancel, on_event=_sink_emitter(sink))  # noqa: SLF001
    pool._plugin_manager = manager  # noqa: SLF001
    await manager.load(plugins)


async def close_pool_plugins(
    pool: EnginePool,
    first: BaseException | None,
) -> BaseException | None:
    """逆序关闭插件；出错只并入「首错」，不阻断其余 pool 资源清理。"""
    manager = pool._plugin_manager  # noqa: SLF001
    if manager is None:
        return first
    try:
        await manager.close()
    except BaseException as exc:  # noqa: BLE001 —— 保留首错，继续清理
        first = first or exc
    return first


__all__ = ["close_pool_plugins", "load_pool_plugins"]
