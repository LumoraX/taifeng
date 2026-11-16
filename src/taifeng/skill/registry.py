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


class SkillIdCollisionError(ValueError):
    """运行期增加的目录里有 skill id 与已加载的 skill 重名（skill-dir-registration）。

    Attributes:
        skill_ids: 重名的 skill id（已排序）。
    """

    def __init__(self, directory: Path, skill_ids: list[str]) -> None:
        """构造错误。"""
        super().__init__(f"skills in {directory} collide with loaded skills: {', '.join(skill_ids)}")
        self.skill_ids = tuple(skill_ids)


class SkillDirHandle:
    """撤销一个经 ``add_dir`` 增加的目录；``aclose`` 幂等。"""

    __slots__ = ("_closed", "_directory", "_registry")

    def __init__(self, registry: FilesystemSkillRegistry, directory: Path) -> None:
        self._registry = registry
        self._directory = directory
        self._closed = False

    @property
    def directory(self) -> Path:
        """被增加的目录（绝对路径）。"""
        return self._directory

    async def aclose(self) -> None:
        """移除目录并重新 discover。"""
        if self._closed:
            return
        self._closed = True
        await self._registry._remove_dir(self._directory)  # noqa: SLF001


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
        # skill-dir-registration：运行期增删的目录、增删串行锁、目录布局版本（watcher 据此重置基线）
        self._added_dirs: set[Path] = set()
        self._layout_lock = asyncio.Lock()
        self._layout_version = 0

    @property
    def layout_version(self) -> int:
        """目录布局版本：每次 ``add_dir`` / 撤销 +1。"""
        return self._layout_version

    async def add_dir(self, path: str | Path, *, source: SkillSource) -> SkillDirHandle:
        """运行期增加一个 skill 目录并重新 discover。

        与构造时多目录的「靠后覆盖靠前」不同：这里的 skill id 与已加载 skill 重名直接拒绝，
        否则增加的目录能悄悄替换已有 skill，撤销后旧 skill 又会「复活」。

        Args:
            path: 目录路径。
            source: 来源信任层级，必填——默认成 ``user`` 会让外来 skill 悄悄拿到用户级信任。

        Returns:
            可撤销 handle；构造时传入的目录没有 handle，不可移除。

        Raises:
            ValueError: ``source`` 非法或目录已加载。
            FileNotFoundError: 目录不存在。
            SkillIdCollisionError: 与已加载 skill 重名（此时不改变任何状态）。
        """
        if source not in get_args(SkillSource):
            raise ValueError(f"unknown skill source {source!r}")
        directory = Path(path).expanduser().resolve()
        async with self._layout_lock:
            if directory in self._skills_dirs:
                raise ValueError(f"skills dir already loaded: {directory}")
            loaded = load_skills_from_dir(directory, source=source)
            clash = sorted(loaded.keys() & self._snapshot.ids())
            if clash:
                raise SkillIdCollisionError(directory, clash)
            self._skills_dirs.append(directory)
            self._sources[directory] = source
            self._added_dirs.add(directory)
            self._layout_version += 1
            try:
                await self.discover()
            except BaseException:
                # discover 失败：撤回布局，保持「失败不改变状态」
                self._forget_dir(directory)
                raise
        return SkillDirHandle(self, directory)

    async def _remove_dir(self, directory: Path) -> None:
        """移除经 ``add_dir`` 增加的目录并重新 discover。"""
        async with self._layout_lock:
            if directory not in self._added_dirs:
                return
            self._forget_dir(directory)
            await self.discover()

    def _forget_dir(self, directory: Path) -> None:
        """从布局中去掉一个运行期目录。"""
        self._skills_dirs.remove(directory)
        self._sources.pop(directory, None)
        self._added_dirs.discard(directory)
        self._layout_version += 1

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
