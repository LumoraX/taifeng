"""Shared pytest fixtures."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


ATOMIC_SKILL = """---
name: style-checker
description: 代码风格审查
version: 1.0.0
type: atomic
---
# 风格审查
按规范审查 diff，列出违规处。
"""

COMPOSITE_SKILL = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是一位代码审查专家。
"""


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(ATOMIC_SKILL, encoding="utf-8")
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE_SKILL, encoding="utf-8")
    return skills


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    p = tmp_path / "threads"
    p.mkdir()
    return p
