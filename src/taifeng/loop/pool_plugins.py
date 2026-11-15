"""EnginePool 的插件接线（plugin-core，ADR 0144）。

从 ``loop/pool.py`` 拆出（该文件逼近 800 行红线）：pool 只透传 ``plugins=`` 并在两处调用本模块——
``create`` 构造出 pool 之后加载，``close`` 清理资源时最先关闭。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from taifeng.hooks.types import HookRunner
from taifeng.loop.event import EventMsg, PluginFailed, PluginLoaded, PluginUnloaded
from taifeng.plugin.capabilities import PluginCapabilities
from taifeng.plugin.errors import PluginCapabilityError
from taifeng.plugin.manager import PluginManager
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.telemetry.fanout import FanoutSink

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.hooks.types import HookRegistry
    from taifeng.loop.pool import EnginePool
    from taifeng.plugin.types import (
        Plugin,
        PluginEventCallback,
        PluginLifecycleEvent,
        RegistrationHandle,
    )
    from taifeng.skill.definition import SkillSource
    from taifeng.skill.registry import SkillDirHandle
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


_AUDIT_REASON = "audit pools validate tools, hooks and skills statically when the pool is built"
_CUSTOM_HOOKS_REASON = "the pool's hooks= is not a taifeng.hooks.HookRunner"
_CUSTOM_SKILLS_REASON = "the pool's skill registry is not a FilesystemSkillRegistry"


def plugin_sink(sink: TelemetrySink | None, plugins: Sequence[Plugin]) -> TelemetrySink | None:
    """传了插件时把 pool 级 sink 包成 ``FanoutSink``（宿主 sink 排第一），供插件追加成员。

    必须在 ``create`` 开头调用：索引钩子 runner 与 SQLite 目录在 pool 构造前就拿走了 sink，
    插件稍后加载时只能往同一个 fan-out 里加成员。未传插件时原样返回，对象身份不变。
    """
    if not plugins:
        return sink
    return FanoutSink(sink) if sink is not None else FanoutSink()


async def _refresh_engines(pool: EnginePool) -> None:
    """让所有活跃 engine 重新取 skill 快照（与 skill watcher 的 ``_on_change`` 同一做法）。"""
    from taifeng.loop.submission import RefreshSnapshot

    async with pool._lock:  # noqa: SLF001
        for engine in pool._engines.values():  # noqa: SLF001
            await engine.submit(RefreshSnapshot())


class _PoolSkillDirHandle:
    """撤销插件挂上的 skill 目录：入口 skill 仍在使用时拒绝，撤销后刷新活跃 engine。"""

    __slots__ = ("_inner", "_pool")

    def __init__(self, pool: EnginePool, inner: SkillDirHandle) -> None:
        self._pool = pool
        self._inner = inner

    async def aclose(self) -> None:
        """移除目录；有活跃 engine 的入口 skill 来自该目录时抛 ``PluginCapabilityError``。"""
        directory = self._inner.directory
        snapshot = self._pool.skill_registry.snapshot()
        for session_id, view in self._pool.introspect().items():
            entry = snapshot.get(str(view.get("entry_skill_id")))
            if entry is not None and entry.body_path.resolve().is_relative_to(directory):
                raise PluginCapabilityError(
                    f"session {session_id!r} entry skill {entry.id!r} lives in {directory}",
                    plugin_id=None,
                    capability="skills",
                )
        await self._inner.aclose()
        await _refresh_engines(self._pool)


class _PoolSkillDirs:
    """pool 侧的 skill 目录后端：委托 ``FilesystemSkillRegistry.add_dir`` 并刷新活跃 engine。"""

    __slots__ = ("_pool", "_registry")

    def __init__(self, pool: EnginePool, registry: FilesystemSkillRegistry) -> None:
        self._pool = pool
        self._registry = registry

    async def add_dir(self, path: str | Path, *, source: SkillSource) -> RegistrationHandle:
        """增加目录并刷新活跃 engine（插件加载期没有 engine，刷新是空操作）。"""
        inner = await self._registry.add_dir(path, source=source)
        await _refresh_engines(self._pool)
        return _PoolSkillDirHandle(self._pool, inner)


def _hook_registry(pool: EnginePool) -> HookRegistry | None:
    """取 pool 的钩子注册表；宿主没传 ``hooks`` 时按需新建 ``HookRunner``。

    回填只发生在插件加载期，此时 pool 还没有任何 engine——engine 创建时才读取 ``pool._hooks``。
    宿主传了别的钩子对象时返回 None（无从注册）。
    """
    hooks = pool._hooks  # noqa: SLF001
    if hooks is None:
        if pool._engines:  # noqa: SLF001
            raise RuntimeError("plugin hooks must be registered before any engine exists")
        hooks = HookRunner()
        pool._hooks = hooks  # noqa: SLF001
    if not isinstance(hooks, HookRunner):
        return None
    return hooks.registry


def _capabilities(pool: EnginePool, sink: TelemetrySink | None) -> PluginCapabilities:
    """按 pool 配置决定可提供的 capability；审计 pool 的静态门已在构造时封住（sink 不受影响）。"""
    telemetry = sink if isinstance(sink, FanoutSink) else None
    if pool._audit is not None:  # noqa: SLF001
        reason = _AUDIT_REASON
        return PluginCapabilities(
            telemetry=telemetry,
            unavailable={"tools": reason, "hooks": reason, "skills": reason},
        )
    unavailable = {}
    if pool._hooks is not None and not isinstance(pool._hooks, HookRunner):  # noqa: SLF001
        unavailable["hooks"] = _CUSTOM_HOOKS_REASON
    registry = pool._registry  # noqa: SLF001
    skills = None
    if isinstance(registry, FilesystemSkillRegistry):
        skills = _PoolSkillDirs(pool, registry)
    else:
        unavailable["skills"] = _CUSTOM_SKILLS_REASON
    return PluginCapabilities(
        tools=pool._tool_registry,  # noqa: SLF001
        hooks=lambda: _hook_registry(pool),
        skills=skills,
        telemetry=telemetry,
        unavailable=unavailable,
    )


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
        sink: pool 级 sink（经 ``plugin_sink`` 包装的 ``FanoutSink``），接收插件生命周期事件，
            也是 ``ctx.telemetry`` 的后端。

    Raises:
        PluginSetupError 等：加载失败时已回滚；调用方负责关闭 pool。
    """
    if not plugins:
        return
    manager = PluginManager(
        cancel=pool._root_cancel,  # noqa: SLF001
        on_event=_sink_emitter(sink),
        capabilities=_capabilities(pool, sink),
    )
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
