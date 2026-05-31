"""SkillFileWatcher 测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from taifeng.skill import FilesystemSkillRegistry, SkillFileWatcher


ATOMIC = """---
name: skill-a
description: x
version: 1.0
type: atomic
---
body
"""

NEW_SKILL = """---
name: skill-b
description: x
version: 1.0
type: atomic
---
body
"""


@pytest.mark.asyncio
async def test_watcher_detects_new_skill(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "skill-a").mkdir(parents=True)
    (skills / "skill-a" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")

    registry = await FilesystemSkillRegistry.load(skills)
    assert len(registry.snapshot().skills) == 1

    snapshots: list = []

    async def on_change(snap) -> None:
        snapshots.append(snap)

    watcher = SkillFileWatcher(registry, poll_interval_seconds=0.05, on_change=on_change)
    task = asyncio.create_task(watcher.run())

    # 等 watcher 初始化
    await asyncio.sleep(0.1)

    # 加新 skill
    (skills / "skill-b").mkdir()
    (skills / "skill-b" / "SKILL.md").write_text(NEW_SKILL, encoding="utf-8")

    # 等触发
    await asyncio.sleep(0.2)
    watcher.stop()
    await task

    assert len(snapshots) >= 1
    final = snapshots[-1]
    assert len(final.skills) == 2
    assert final.get("skill-b") is not None


@pytest.mark.asyncio
async def test_watcher_no_change_no_callback(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "skill-a").mkdir(parents=True)
    (skills / "skill-a" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
    registry = await FilesystemSkillRegistry.load(skills)

    snapshots: list = []

    async def on_change(snap) -> None:
        snapshots.append(snap)

    watcher = SkillFileWatcher(registry, poll_interval_seconds=0.05, on_change=on_change)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0.2)  # 无变更
    watcher.stop()
    await task

    assert len(snapshots) == 0
