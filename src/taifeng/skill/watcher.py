"""SKILL.md 文件监听 —— 增量热更新 SkillSnapshot。

设计：mtime 轮询（默认 1 秒），无第三方依赖（watchdog/inotify-binding 跨平台问题多）。
变更后整体重扫一次（21 个 skill 约 150ms，可接受）；如果未来量大切换到 watchfiles。

策略：
    - 增量检测：只比对 SKILL.md 文件的 mtime
    - 变更后调 ``registry.discover()`` 重建快照
    - Snapshot 版本号 +1，已存在的 Engine 不感知（lock-in 语义；显式 refresh_skills() 才换）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from taifeng.skill.registry import FilesystemSkillRegistry, SkillSnapshot

logger = logging.getLogger(__name__)


class SkillFileWatcher:
    """轮询型 SKILL.md 监听。

    用法：
        watcher = SkillFileWatcher(registry, poll_interval_seconds=1.0)
        task = asyncio.create_task(watcher.run())
        ...
        watcher.stop()
        await task
    """

    def __init__(
        self,
        registry: FilesystemSkillRegistry,
        *,
        poll_interval_seconds: float = 1.0,
        on_change: Callable[[SkillSnapshot], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._interval = poll_interval_seconds
        self._on_change = on_change
        self._stop = asyncio.Event()
        # path -> mtime 缓存
        self._mtimes: dict[Path, float] = {}
        self._initialized = False

    def _scan_mtimes(self) -> dict[Path, float]:
        result: dict[Path, float] = {}
        for d in self._registry._skills_dirs:  # noqa: SLF001
            if not d.exists():
                continue
            for skill_md in d.glob("*/SKILL.md"):
                try:
                    result[skill_md] = skill_md.stat().st_mtime
                except OSError:
                    continue
        return result

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """主循环。"""
        self._mtimes = self._scan_mtimes()
        self._initialized = True
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                break  # stop signaled
            except TimeoutError:
                pass

            current = self._scan_mtimes()
            if current != self._mtimes:
                changed = self._diff(self._mtimes, current)
                logger.info("SKILL.md change detected: %s", changed)
                self._mtimes = current
                try:
                    snap = await self._registry.discover()
                    if self._on_change is not None:
                        await self._on_change(snap)
                except Exception:
                    logger.exception("registry.discover failed after file change")

    @staticmethod
    def _diff(prev: dict[Path, float], curr: dict[Path, float]) -> dict[str, list[str]]:
        prev_keys = set(prev.keys())
        curr_keys = set(curr.keys())
        added = curr_keys - prev_keys
        removed = prev_keys - curr_keys
        modified = {p for p in curr_keys & prev_keys if curr[p] != prev[p]}
        return {
            "added": sorted(str(p) for p in added),
            "removed": sorted(str(p) for p in removed),
            "modified": sorted(str(p) for p in modified),
        }
