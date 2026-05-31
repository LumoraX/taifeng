"""T2 — Loader 对 SKILL.md ``scripts`` frontmatter 的扩展。

覆盖：
1. frontmatter 显式声明 → 完整 ScriptDescriptor
2. 隐式发现（无 frontmatter，但目录有 scripts/*.sh） → 默认 timeout 60 / args_schema={}
3. 显式 + 隐式同名 → 显式覆盖
4. path 越权（``../../``）→ SkillValidationError
5. 显式声明缺 ``timeout_seconds`` → SkillValidationError
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.skill.definition import SkillValidationError
from taifeng.skill.loader import load_skills_from_dir


def _write_skill_md(skill_dir: Path, frontmatter: str, body: str = "body") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter.strip()}\n---\n{body}\n",
        encoding="utf-8",
    )


def _write_script(skill_dir: Path, relpath: str, content: str = "#!/bin/sh\necho hi\n") -> Path:
    p = skill_dir / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    p.chmod(0o755)
    return p


def test_explicit_script_declaration(tmp_path: Path) -> None:
    """frontmatter 显式声明被完整解析。"""
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/normalize.sh")
    _write_skill_md(
        skill,
        """
name: data-prep
description: 数据预处理
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
    timeout_seconds: 30
    description: 把 CSV 标准化
    args_schema:
      type: object
      properties:
        input: {type: string}
      required: [input]
    max_output_bytes: 8192
""",
    )
    skills = load_skills_from_dir(tmp_path)
    assert "data-prep" in skills
    scripts = skills["data-prep"].scripts
    assert len(scripts) == 1
    s = scripts[0]
    assert s.name == "normalize"
    assert s.language == "shell"
    assert s.timeout_seconds == 30.0
    assert s.max_output_bytes == 8192
    assert s.description == "把 CSV 标准化"
    assert s.args_schema["required"] == ["input"]
    # path 已解析为绝对路径
    assert s.path.is_absolute()
    assert s.path.name == "normalize.sh"


def test_implicit_discovery_when_no_frontmatter(tmp_path: Path) -> None:
    """frontmatter 没 scripts 字段时，scripts/ 目录被自动扫描。"""
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/foo.sh")
    _write_script(skill, "scripts/bar.py", "#!/usr/bin/env python\nprint('ok')\n")
    _write_script(skill, "scripts/baz.js", "console.log('hi');\n")
    _write_skill_md(skill, "name: data-prep\ndescription: x")
    skills = load_skills_from_dir(tmp_path)
    names = {s.name for s in skills["data-prep"].scripts}
    assert names == {"foo", "bar", "baz"}
    by_name = {s.name: s for s in skills["data-prep"].scripts}
    assert by_name["foo"].language == "shell"
    assert by_name["bar"].language == "python"
    assert by_name["baz"].language == "custom"
    # 隐式发现默认 timeout=60
    assert by_name["foo"].timeout_seconds == 60.0
    assert by_name["foo"].args_schema == {"type": "object"}


def test_explicit_overrides_implicit_same_name(tmp_path: Path) -> None:
    """同名 script 显式声明覆盖隐式发现。"""
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/normalize.sh")
    _write_skill_md(
        skill,
        """
name: data-prep
description: x
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
    timeout_seconds: 120
    description: 覆盖隐式
""",
    )
    skills = load_skills_from_dir(tmp_path)
    scripts = skills["data-prep"].scripts
    assert len(scripts) == 1
    assert scripts[0].timeout_seconds == 120.0
    assert scripts[0].description == "覆盖隐式"


def test_explicit_and_implicit_coexist_different_names(tmp_path: Path) -> None:
    """显式声明 + 隐式发现可共存，不同 name 各自存在。"""
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/normalize.sh")
    _write_script(skill, "scripts/cleanup.py", "print('ok')\n")
    _write_skill_md(
        skill,
        """
name: data-prep
description: x
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
    timeout_seconds: 30
""",
    )
    skills = load_skills_from_dir(tmp_path)
    names = {s.name for s in skills["data-prep"].scripts}
    assert names == {"normalize", "cleanup"}
    by_name = {s.name: s for s in skills["data-prep"].scripts}
    assert by_name["normalize"].timeout_seconds == 30.0
    assert by_name["cleanup"].timeout_seconds == 60.0  # 隐式默认


def test_script_path_outside_skill_dir_rejected(tmp_path: Path) -> None:
    """``../`` 越权路径被 loader 拒绝。"""
    skill = tmp_path / "evil"
    # 在 tmp_path 而非 skill 目录下放脚本
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\necho leak\n", encoding="utf-8")
    _write_skill_md(
        skill,
        """
name: evil
description: x
scripts:
  - name: leak
    path: ../outside.sh
    language: shell
    timeout_seconds: 30
""",
    )
    with pytest.raises(SkillValidationError, match="script path"):
        load_skills_from_dir(tmp_path)


def test_explicit_missing_timeout_rejected(tmp_path: Path) -> None:
    """显式声明缺 timeout_seconds → 立即失败。"""
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/normalize.sh")
    _write_skill_md(
        skill,
        """
name: data-prep
description: x
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
""",
    )
    with pytest.raises(SkillValidationError, match="timeout_seconds"):
        load_skills_from_dir(tmp_path)


def test_explicit_unknown_language_rejected(tmp_path: Path) -> None:
    skill = tmp_path / "data-prep"
    _write_script(skill, "scripts/normalize.sh")
    _write_skill_md(
        skill,
        """
name: data-prep
description: x
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: ruby
    timeout_seconds: 30
""",
    )
    with pytest.raises(SkillValidationError, match="language"):
        load_skills_from_dir(tmp_path)


def test_no_scripts_dir_empty_tuple(tmp_path: Path) -> None:
    skill = tmp_path / "empty"
    _write_skill_md(skill, "name: empty\ndescription: x")
    skills = load_skills_from_dir(tmp_path)
    assert skills["empty"].scripts == ()


def test_loader_parses_orchestration(tmp_path) -> None:
    """composite skill 的 orchestration frontmatter 被解析为 OrchestrationSpec。"""
    from taifeng.skill.loader import load_skills_from_dir
    from taifeng.skill.orchestration import ParallelStep, SerialStep

    skills = tmp_path / "s"
    entry = skills / "trip"
    entry.mkdir(parents=True)
    (entry / "SKILL.md").write_text(
        "---\n"
        "name: trip\ndescription: 行程\nversion: 1.0.0\n"
        "type: composite\nentry: true\n"
        "child_skills: [route-a, summarizer]\n"
        "orchestration:\n"
        "  steps:\n"
        "    - parallel: [route-a]\n"
        "    - serial: [summarizer]\n"
        "---\n# trip\n",
        encoding="utf-8",
    )
    for sid in ("route-a", "summarizer"):
        d = skills / sid
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {sid}\ndescription: d\nversion: 1.0.0\ntype: atomic\n---\n# {sid}\n",
            encoding="utf-8",
        )

    loaded = load_skills_from_dir(skills)
    spec = loaded["trip"].orchestration
    assert spec is not None
    assert isinstance(spec.steps[0], ParallelStep)
    assert isinstance(spec.steps[1], SerialStep)


def test_loader_orchestration_unknown_id_fails_fast(tmp_path) -> None:
    """orchestration 引用不在 child_skills 的 id → 加载期 SkillValidationError（fail-fast）。"""
    from taifeng.skill.definition import SkillValidationError
    from taifeng.skill.loader import load_skills_from_dir

    skills = tmp_path / "s"
    entry = skills / "trip"
    entry.mkdir(parents=True)
    (entry / "SKILL.md").write_text(
        "---\n"
        "name: trip\ndescription: 行程\nversion: 1.0.0\n"
        "type: composite\nentry: true\n"
        "child_skills: [route-a]\n"
        "orchestration:\n  steps:\n    - parallel: [route-a, ghost]\n"
        "---\n# trip\n",
        encoding="utf-8",
    )
    d = skills / "route-a"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: route-a\ndescription: d\nversion: 1.0.0\ntype: atomic\n---\n# r\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError, match="不在 child_skills"):
        load_skills_from_dir(skills)


def test_loader_atomic_orchestration_fails_fast(tmp_path) -> None:
    """atomic skill 带 orchestration → 加载期报错（清晰信息，非白名单误报）。"""
    from taifeng.skill.definition import SkillValidationError
    from taifeng.skill.loader import load_skills_from_dir

    skills = tmp_path / "s"
    d = skills / "lonely"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: lonely\ndescription: d\nversion: 1.0.0\ntype: atomic\n"
        "orchestration:\n  steps:\n    - parallel: [x]\n---\n# l\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError, match="不能声明 orchestration"):
        load_skills_from_dir(skills)
