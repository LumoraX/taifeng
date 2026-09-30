"""SkillRegistry —— skill 注册表与不可变快照。

参照：
    - 内部已实测 Python 实现（registry / watcher 范式）
    - codex codex-rs/core-skills/src/manager.rs
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, get_args, runtime_checkable

from taifeng.skill.definition import SkillSource
from taifeng.skill.loader import compute_reachable_graph, load_skills_from_dir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

    from taifeng.skill.definition import SkillDefinition

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


def _resolve_sources(
    skills_dirs: list[Path],
    sources: Mapping[str | Path, SkillSource] | None,
) -> dict[Path, SkillSource]:
    """把「目录 → 来源」的声明解析成绝对路径键；声明了未加载的目录或非法来源即报错。"""
    resolved: dict[Path, SkillSource] = {}
    for raw, source in (sources or {}).items():
        directory = Path(raw).expanduser().resolve()
        if directory not in skills_dirs:
            raise ValueError(f"source declared for a directory that is not loaded: {raw}")
        if source not in get_args(SkillSource):
            raise ValueError(f"unknown skill source {source!r} for {raw}")
        resolved[directory] = source
    return resolved


class FilesystemSkillRegistry(SkillRegistry):
    """从文件系统加载并维护 SkillSnapshot 的内存注册表。

    使用：
        registry = await FilesystemSkillRegistry.load("/data/skills")
        snap = registry.snapshot()
    """

    def __init__(
        self,
        skills_dirs: Iterable[Path],
        *,
        sources: Mapping[str | Path, SkillSource] | None = None,
    ) -> None:
        """
        Args:
            skills_dirs: skill 目录，靠后的覆盖靠前的。
            sources: 目录 → 来源（``system`` / ``user`` / ``marketplace``），决定该目录下
                skill 的 ``SkillDefinition.source``；没有列出的目录为 ``user``。来源信任
                分层（``SkillTrustPolicy``）据此判定层级。

        Raises:
            ValueError: ``sources`` 里的目录不在 ``skills_dirs`` 内，或来源取值非法。
        """
        self._skills_dirs = [Path(p).expanduser().resolve() for p in skills_dirs]
        self._sources = _resolve_sources(self._skills_dirs, sources)
        self._snapshot = SkillSnapshot(version=0, skills=())
        self._version_counter = 0
        self._watchers: list[asyncio.Queue[SkillSnapshot]] = []

    @classmethod
    async def load(
        cls,
        skills_dir: str | Path | Iterable[str | Path],
        *,
        sources: Mapping[str | Path, SkillSource] | None = None,
    ) -> FilesystemSkillRegistry:
        """从一个或多个目录加载 skills；``sources`` 见构造函数。"""
        if isinstance(skills_dir, (str, Path)):
            dirs = [Path(skills_dir)]
        else:
            dirs = [Path(p) for p in skills_dir]
        registry = cls(dirs, sources=sources)
        await registry.discover()
        return registry

    async def discover(self) -> SkillSnapshot:
        """全量扫描所有 skills_dirs，构建新快照。"""
        all_skills: dict[str, SkillDefinition] = {}
        for d in self._skills_dirs:
            if not d.exists():
                logger.warning("skills_dir not found: %s", d)
                continue
            loaded = load_skills_from_dir(d, source=self._sources.get(d, "user"))
            # 后加载的覆盖先加载的（分层语义：靠后的目录覆盖靠前的）。覆盖是有意为之，
            # 但必须可见：同名 skill 静默替换会让作者改了 A 目录却看不到效果。
            for skill_id in sorted(loaded.keys() & all_skills.keys()):
                logger.warning(
                    "skill %r from %s overrides the one from %s",
                    skill_id, loaded[skill_id].body_path, all_skills[skill_id].body_path,
                )
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
