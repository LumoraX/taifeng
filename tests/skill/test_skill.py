"""SkillDefinition / Registry / loader 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.skill import (
    CircularSkillReference,
    DispatchPolicy,
    FilesystemSkillRegistry,
    SkillDefinition,
    SkillValidationError,
    UnknownChildSkill,
    detect_cycles,
)
from taifeng.skill.loader import load_skills_from_dir


@pytest.mark.asyncio
async def test_load_atomic_and_composite(skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    snap = registry.snapshot()
    assert len(snap.skills) == 2
    assert len(snap.entries()) == 1
    entry = snap.entries()[0]
    assert entry.id == "code-reviewer"
    assert entry.type == "composite"
    assert "style-checker" in entry.child_skills


def test_atomic_with_child_skills_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        """---
name: bad
description: x
type: atomic
child_skills: [other]
---
body
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError):
        load_skills_from_dir(tmp_path)


def test_composite_missing_child_skills_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        """---
name: bad
description: x
type: composite
---
body
""",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError):
        load_skills_from_dir(tmp_path)


def test_unknown_child_skill_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        """---
name: bad
description: x
type: composite
entry: true
child_skills: [does-not-exist]
---
body
""",
        encoding="utf-8",
    )
    with pytest.raises(UnknownChildSkill):
        load_skills_from_dir(tmp_path)


def test_static_cycle_detection(tmp_path: Path) -> None:
    """a → b → a 环应该被启动期捕获。"""
    for name, children in [("a", ["b"]), ("b", ["a"])]:
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"""---
name: {name}
description: x
type: composite
entry: true
child_skills: [{children[0]}]
---
body
""",
            encoding="utf-8",
        )
    with pytest.raises(CircularSkillReference):
        load_skills_from_dir(tmp_path)


def test_detect_cycles_unit() -> None:
    """直接测 detect_cycles 算法。"""
    a = SkillDefinition(
        id="a", name="a", description="x", version="1", body="", body_path=Path("/tmp"),
        type="composite", child_skills=frozenset({"b"}),
    )
    b = SkillDefinition(
        id="b", name="b", description="x", version="1", body="", body_path=Path("/tmp"),
        type="composite", child_skills=frozenset({"c"}),
    )
    c = SkillDefinition(
        id="c", name="c", description="x", version="1", body="", body_path=Path("/tmp"),
        type="composite", child_skills=frozenset({"a"}),  # 长环 a→b→c→a
    )
    cycles = detect_cycles({"a": a, "b": b, "c": c})
    assert len(cycles) == 1
    # 环含 a, b, c
    cycle_set = set(cycles[0])
    assert {"a", "b", "c"}.issubset(cycle_set)


@pytest.mark.asyncio
async def test_reachable_graph(skills_dir: Path) -> None:
    registry = await FilesystemSkillRegistry.load(skills_dir)
    snap = registry.snapshot()
    reachable = snap.reachable_from("code-reviewer")
    assert "style-checker" in reachable
    assert "code-reviewer" in reachable


def test_atomic_with_orchestration_rejected(tmp_path: Path) -> None:
    """atomic skill 声明 orchestration → validate 抛 SkillValidationError。"""
    from taifeng.skill.definition import SkillDefinition, SkillValidationError
    from taifeng.skill.orchestration import OrchestrationSpec, ParallelStep

    defn = SkillDefinition(
        id="x", name="x", description="d", version="1.0.0",
        body="b", body_path=tmp_path / "x.md", type="atomic",
        orchestration=OrchestrationSpec(steps=(ParallelStep(skill_ids=("a",)),)),
    )
    with pytest.raises(SkillValidationError, match="不能声明 orchestration"):
        defn.validate()


def test_composite_with_orchestration_ok(tmp_path: Path) -> None:
    """composite skill 带 orchestration → validate 通过。"""
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.orchestration import OrchestrationSpec, ParallelStep

    defn = SkillDefinition(
        id="trip", name="trip", description="d", version="1.0.0",
        body="b", body_path=tmp_path / "trip.md", type="composite",
        child_skills=frozenset({"a"}),
        orchestration=OrchestrationSpec(steps=(ParallelStep(skill_ids=("a",)),)),
    )
    defn.validate()  # 不抛即通过


def test_tool_only_composite_accepted(tmp_path: Path) -> None:
    """composite 仅声明 tool_names（无 child_skills）应通过校验 —— 变体 A：有 agency 即合法。"""
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "SKILL.md").write_text(
        """---
name: ok
description: x
type: composite
tool_names: [request_user_input]
---
body
""",
        encoding="utf-8",
    )
    skills = load_skills_from_dir(tmp_path)
    # load_skills_from_dir 返回 {id: SkillDefinition}；tool-only composite 正常载入
    assert skills["ok"].type == "composite"
    assert "request_user_input" in skills["ok"].tool_names
    assert not skills["ok"].child_skills
