"""``FilesystemSkillRegistry.add_dir``：运行期增删 skill 目录（skill-dir-registration）。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.skill.registry import FilesystemSkillRegistry, SkillIdCollisionError
from taifeng.skill.watcher import SkillFileWatcher

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, skill_id: str, *, children: tuple[str, ...] = ()) -> Path:
    """在 root 下写一个 skill，返回 root。"""
    (root / skill_id).mkdir(parents=True, exist_ok=True)
    head = "type: composite\nentry: true\n" if children else "type: atomic\n"
    child_line = f"child_skills: [{', '.join(children)}]\n" if children else ""
    (root / skill_id / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: d\n{head}{child_line}---\n# {skill_id}\n",
        encoding="utf-8",
    )
    return root


async def test_add_dir_then_remove(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    extra = _write_skill(tmp_path / "extra", "plugin-skill")
    v0 = registry.snapshot().version
    layout0 = registry.layout_version
    handle = await registry.add_dir(extra, source="marketplace")
    skill = registry.get("plugin-skill")
    assert skill is not None and skill.source == "marketplace"
    assert registry.snapshot().version == v0 + 1
    assert registry.layout_version == layout0 + 1
    assert handle.directory == extra.resolve()
    await handle.aclose()
    assert registry.get("plugin-skill") is None
    assert registry.snapshot().version == v0 + 2
    await handle.aclose()  # 幂等
    assert registry.snapshot().version == v0 + 2


async def test_collision_rejected_without_state_change(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    clash = _write_skill(tmp_path / "clash", "style-checker")  # skills_dir 里已有
    _write_skill(clash, "fresh")
    before = (registry.snapshot().version, registry.layout_version, registry.snapshot().ids())
    with pytest.raises(SkillIdCollisionError) as caught:
        await registry.add_dir(clash, source="marketplace")
    assert caught.value.skill_ids == ("style-checker",)
    assert (registry.snapshot().version, registry.layout_version, registry.snapshot().ids()) == before


@pytest.mark.parametrize("source", ["", "trusted", None])
async def test_invalid_source_rejected(tmp_path: Path, skills_dir: Path, source: object) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    extra = _write_skill(tmp_path / "extra", "x")
    with pytest.raises(ValueError):
        await registry.add_dir(extra, source=source)  # type: ignore[arg-type]
    assert registry.get("x") is None


async def test_missing_or_duplicate_directory_rejected(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    with pytest.raises(FileNotFoundError):
        await registry.add_dir(tmp_path / "nope", source="user")
    with pytest.raises(ValueError):
        await registry.add_dir(skills_dir, source="user")  # 构造时已加载


async def test_concurrent_add_dir_is_serialized(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    a = _write_skill(tmp_path / "a", "skill-a")
    b = _write_skill(tmp_path / "b", "skill-b")
    v0 = registry.snapshot().version
    await asyncio.gather(registry.add_dir(a, source="user"), registry.add_dir(b, source="user"))
    assert {"skill-a", "skill-b"} <= registry.snapshot().ids()
    assert registry.snapshot().version == v0 + 2


async def test_concurrent_same_id_one_wins(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    a = _write_skill(tmp_path / "a", "same")
    b = _write_skill(tmp_path / "b", "same")
    results = await asyncio.gather(
        registry.add_dir(a, source="user"), registry.add_dir(b, source="user"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SkillIdCollisionError) for r in results) == 1


async def test_removed_children_become_unreachable(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    extra = tmp_path / "extra"
    _write_skill(extra, "leaf")
    _write_skill(extra, "parent", children=("leaf",))
    handle = await registry.add_dir(extra, source="user")
    assert "leaf" in registry.snapshot().reachable_from("parent")
    await handle.aclose()
    assert registry.snapshot().reachable_from("parent") == frozenset({"parent"})
    assert registry.get("leaf") is None


async def test_watcher_does_not_rediscover_after_add_dir(tmp_path: Path, skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    changes: list[int] = []

    async def on_change(snapshot: object) -> None:
        changes.append(1)

    watcher = SkillFileWatcher(registry, poll_interval_seconds=0.02, on_change=on_change)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0.05)
    await registry.add_dir(_write_skill(tmp_path / "extra", "plugin-skill"), source="user")
    await asyncio.sleep(0.15)
    watcher.stop()
    await task
    assert changes == []  # add_dir 已 discover，watcher 只重置基线
