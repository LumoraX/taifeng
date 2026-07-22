"""SkillRegistry —— skill 注册表与不可变快照。

参照：
    - 内部已实测 Python 实现（registry / watcher 范式）
    - codex codex-rs/core-skills/src/manager.rs
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from taifeng.skill.definition import SkillDefinition
from taifeng.skill.loader import compute_reachable_graph, load_skills_from_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSnapshot:
    """注册表不可变快照。

    所有 Engine 都持有此对象的引用（不复制），由 `version` 标识版本。
    """

    version: int
    skills: tuple[SkillDefinition, ...]
    reachable_graph: dict[str, frozenset[str]] = field(default_factory=dict)

    def get(self, skill_id: str) -> SkillDefinition | None:
        for s in self.skills:
            if s.id == skill_id:
                return s
        return None

    def entries(self) -> tuple[SkillDefinition, ...]:
        """所有 ``entry=true`` 的 skill。"""
        return tuple(s for s in self.skills if s.entry)

    def ids(self) -> frozenset[str]:
        return frozenset(s.id for s in self.skills)

    def reachable_from(self, entry_id: str) -> frozenset[str]:
        """从 entry skill 出发可达的全部子 skill（含自身）。"""
        return self.reachable_graph.get(entry_id, frozenset()) | {entry_id}


@runtime_checkable
class SkillRegistry(Protocol):
    """skill 注册表协议。"""

    async def discover(self) -> SkillSnapshot:
        """全量重扫，返回最新快照。"""
        ...

    def snapshot(self) -> SkillSnapshot:
        """O(1) 返回当前快照。"""
        ...

    def get(self, skill_id: str) -> SkillDefinition | None:
        ...

    def watch(self) -> AsyncIterator[SkillSnapshot]:
        """订阅快照变更（可选实现）。"""
        ...


class FilesystemSkillRegistry(SkillRegistry):
    """从文件系统加载并维护 SkillSnapshot 的内存注册表。

    使用：
        registry = await FilesystemSkillRegistry.load("/data/skills")
        snap = registry.snapshot()
    """

    def __init__(self, skills_dirs: Iterable[Path]) -> None:
        self._skills_dirs = [Path(p).expanduser().resolve() for p in skills_dirs]
        self._snapshot = SkillSnapshot(version=0, skills=())
        self._version_counter = 0
        self._watchers: list[asyncio.Queue[SkillSnapshot]] = []

    @classmethod
    async def load(
        cls,
        skills_dir: str | Path | Iterable[str | Path],
    ) -> FilesystemSkillRegistry:
        """从一个或多个目录加载 skills。"""
        if isinstance(skills_dir, (str, Path)):
            dirs = [Path(skills_dir)]
        else:
            dirs = [Path(p) for p in skills_dir]
        registry = cls(dirs)
        await registry.discover()
        return registry

    async def discover(self) -> SkillSnapshot:
        """全量扫描所有 skills_dirs，构建新快照。"""
        all_skills: dict[str, SkillDefinition] = {}
        for d in self._skills_dirs:
            if not d.exists():
                logger.warning("skills_dir not found: %s", d)
                continue
            loaded = load_skills_from_dir(d, source="user")
            # 后加载的覆盖先加载的（用户目录覆盖系统目录的语义）
            all_skills.update(loaded)

        reachable = compute_reachable_graph(all_skills)
        self._version_counter += 1
        self._snapshot = SkillSnapshot(
            version=self._version_counter,
            skills=tuple(all_skills.values()),
            reachable_graph=reachable,
        )
        logger.info(
            "skill discovery complete: version=%d, total=%d, entries=%d",
            self._snapshot.version,
            len(self._snapshot.skills),
            len(self._snapshot.entries()),
        )
        # 通知 watchers
        for q in self._watchers:
            try:
                q.put_nowait(self._snapshot)
            except asyncio.QueueFull:
                pass
        return self._snapshot

    def snapshot(self) -> SkillSnapshot:
        return self._snapshot

    def get(self, skill_id: str) -> SkillDefinition | None:
        return self._snapshot.get(skill_id)

    async def watch(self) -> AsyncIterator[SkillSnapshot]:
        q: asyncio.Queue[SkillSnapshot] = asyncio.Queue(maxsize=16)
        self._watchers.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._watchers.remove(q)
