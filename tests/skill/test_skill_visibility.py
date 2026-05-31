"""G4：skill 可见性治理 —— requires 资格门控 + exposure 曝光拆分。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.skill.eligibility import RuntimeCapabilities, is_skill_eligible
from taifeng.skill.loader import load_skills_from_dir

_ENTRY = """---
name: orchestrator
description: 编排
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [needs-jq, hidden-skill, plain]
max_call_depth: 3
---
# 编排
"""

_NEEDS_JQ = """---
name: needs-jq
description: 需要 jq
version: 1.0.0
type: atomic
requires:
  bins: [jq]
  os: [linux, darwin]
---
# needs jq
"""

_HIDDEN = """---
name: hidden-skill
description: 对模型隐藏
version: 1.0.0
type: atomic
exposure:
  model_invocable: false
  user_invocable: true
---
# hidden
"""

_PLAIN = """---
name: plain
description: 普通
version: 1.0.0
type: atomic
---
# plain
"""


def _write_skills(root: Path) -> Path:
    skills = root / "skills"
    for name, content in (
        ("orchestrator", _ENTRY),
        ("needs-jq", _NEEDS_JQ),
        ("hidden-skill", _HIDDEN),
        ("plain", _PLAIN),
    ):
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    return skills


def test_loader_parses_requires_and_exposure(tmp_path: Path) -> None:
    skills = load_skills_from_dir(_write_skills(tmp_path))
    jq = skills["needs-jq"]
    assert jq.requires.bins == frozenset({"jq"})
    assert jq.requires.os == frozenset({"linux", "darwin"})
    assert not jq.requires.is_empty()

    hidden = skills["hidden-skill"]
    assert hidden.exposure.model_invocable is False
    assert hidden.exposure.user_invocable is True

    plain = skills["plain"]
    assert plain.requires.is_empty()
    assert plain.exposure.model_invocable is True


def test_is_skill_eligible(tmp_path: Path) -> None:
    skills = load_skills_from_dir(_write_skills(tmp_path))
    jq = skills["needs-jq"]
    plain = skills["plain"]

    # 空要求恒 True
    assert is_skill_eligible(plain, RuntimeCapabilities())

    # bin 缺失 → False
    caps_no_jq = RuntimeCapabilities(available_bins=frozenset(), os_name="linux")
    assert is_skill_eligible(jq, caps_no_jq) is False

    # bin 有 + os 匹配 → True
    caps_ok = RuntimeCapabilities(
        available_bins=frozenset({"jq", "rg"}), os_name="darwin"
    )
    assert is_skill_eligible(jq, caps_ok) is True

    # os 不匹配 → False
    caps_win = RuntimeCapabilities(
        available_bins=frozenset({"jq"}), os_name="windows"
    )
    assert is_skill_eligible(jq, caps_win) is False


def test_render_prompt_hides_model_invocable_false(tmp_path: Path) -> None:
    from taifeng.loop.prompt import render_system_prompt
    from taifeng.skill.registry import FilesystemSkillRegistry

    import anyio

    async def _load() -> object:
        reg = await FilesystemSkillRegistry.load(_write_skills(tmp_path))
        return reg

    reg = anyio.run(_load)
    snap = reg.snapshot()
    entry = reg.get("orchestrator")
    assert entry is not None

    # 无 capabilities：hidden-skill 因 model_invocable=False 被隐藏；
    # needs-jq 仍出现（未提供 caps → 不做资格过滤）；plain 出现
    prompt = render_system_prompt(entry, snap)
    assert "hidden-skill" not in prompt
    assert "needs-jq" in prompt
    assert "plain" in prompt


def test_render_prompt_filters_ineligible_with_caps(tmp_path: Path) -> None:
    from taifeng.loop.prompt import render_system_prompt
    from taifeng.skill.registry import FilesystemSkillRegistry

    import anyio

    reg = anyio.run(
        lambda: FilesystemSkillRegistry.load(_write_skills(tmp_path))
    )
    snap = reg.snapshot()
    entry = reg.get("orchestrator")
    assert entry is not None

    # 提供 caps 但缺 jq → needs-jq 被资格过滤掉；plain 仍在；hidden 仍隐藏
    caps = RuntimeCapabilities(available_bins=frozenset(), os_name="linux")
    prompt = render_system_prompt(entry, snap, capabilities=caps)
    assert "needs-jq" not in prompt
    assert "hidden-skill" not in prompt
    assert "plain" in prompt
