"""把工具注册表的变更广播成各 engine 上的 ``tool_set_changed`` 事件（R3，dynamic-tool-set）。

注册表是 pool 级共享的（所有 session 共用同一张工具表），而事件总线是 engine 级的；
本模块在两者之间搭桥：注册表同步回调 → 为每个活跃 engine 调度一次异步 emit。

独立成模块是为了不再给已超 800 行红线的 ``pool.py`` 增加实现体（pool 只负责一行接线）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from taifeng.loop.event import EventMsg, ToolSetChanged

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from taifeng.loop.engine import AgentEngine
    from taifeng.tool.registry import ToolRegistry, ToolSetChange

logger = logging.getLogger(__name__)


def bind_tool_set_events(
    registry: ToolRegistry,
    engines: Callable[[], Mapping[str, AgentEngine]],
) -> Callable[[], None]:
    """订阅注册表变更，向当时全部活跃 engine emit ``tool_set_changed``。

    Args:
        registry: pool 共享的工具注册表。
        engines: 取当前活跃 engine 映射的回调（pool 的 ``_engines`` 视图）。

    Returns:
        退订函数（pool 关闭时调用）。

    注册表回调是同步的，emit 是协程：在有活跃 engine 时（此时必在 event loop 内）
    以 task 调度，task 引用保存在集合里直到完成（防被 GC 提前回收）。没有活跃 engine
    时无人可收，直接返回。
    """
    pending: set[asyncio.Task[None]] = set()

    def _on_change(change: ToolSetChange) -> None:
        targets = list(engines().values())
        if not targets:
            return
        loop = asyncio.get_running_loop()
        for engine in targets:
            msg = EventMsg(submission_id="*", msg=ToolSetChanged(data=change.as_dict()))
            task = loop.create_task(engine._emit(msg))  # noqa: SLF001
            pending.add(task)
            task.add_done_callback(pending.discard)

    return registry.subscribe(_on_change)


__all__ = ["bind_tool_set_events"]
