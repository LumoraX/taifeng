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


_RECALL_DEFERRED = """---
name: recall-deferred
description: 显式声明 deferred 召回
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [plain]
exposure:
  child_recall: deferred
---
# deferred
"""

_RECALL_INLINE = """---
name: recall-inline
description: 显式声明 inline 召回
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [plain]
exposure:
  child_recall: inline
---
# inline
"""

_RECALL_BAD = """---
name: recall-bad
description: 非法 child_recall
version: 1.0.0
type: atomic
exposure:
  child_recall: lazy
---
# bad
"""


def test_child_recall_default_is_auto(tmp_path: Path) -> None:
    """缺 child_recall（甚至缺整个 exposure）→ 默认 ``auto``。"""
    skills = load_skills_from_dir(_write_skills(tmp_path))
    # plain 无 exposure 段
    assert skills["plain"].exposure.child_recall == "auto"
    # hidden-skill 有 exposure 但无 child_recall 字段
    assert skills["hidden-skill"].exposure.child_recall == "auto"


def test_child_recall_parses_explicit_values(tmp_path: Path) -> None:
    """显式 inline / deferred / auto 原样解析。"""
    skills = root = tmp_path / "skills"
    for name, content in (
        ("recall-deferred", _RECALL_DEFERRED),
        ("recall-inline", _RECALL_INLINE),
        ("plain", _PLAIN),
    ):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    skills = load_skills_from_dir(root)
    assert skills["recall-deferred"].exposure.child_recall == "deferred"
    assert skills["recall-inline"].exposure.child_recall == "inline"


def test_child_recall_illegal_value_raises(tmp_path: Path) -> None:
    """非法 child_recall（``lazy``）→ 加载抛 SkillValidationError，禁 silent fallback。"""
    from taifeng.skill.definition import SkillValidationError

    root = tmp_path / "skills"
    d = root / "recall-bad"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_RECALL_BAD, encoding="utf-8")

    with pytest.raises(SkillValidationError) as exc_info:
        load_skills_from_dir(root)
    msg = str(exc_info.value)
    # 异常信息必须含字段名 + 非法值，便于定位
    assert "child_recall" in msg
    assert "lazy" in msg
    assert "recall-bad" in msg


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


# ============================================================================
# T6 effective_child_recall：inline / deferred 生效模式裁定（单一真相）
# ============================================================================

from taifeng.skill.definition import SkillDefinition, SkillExposure
from taifeng.skill.visibility import effective_child_recall


def _make_entry(child_recall: str, child_skills: frozenset[str]) -> SkillDefinition:
    """构造一个最小 composite entry，仅用于 effective_child_recall 单测。

    只填判定所需字段（exposure.child_recall + child_skills）；其余取默认。
    """
    return SkillDefinition(
        id="e",
        name="e",
        description="测试 entry",
        version="1.0.0",
        body="# e",
        body_path=Path("/tmp/e/SKILL.md"),
        type="composite",
        entry=True,
        child_skills=child_skills,
        exposure=SkillExposure(child_recall=child_recall),  # type: ignore[arg-type]
    )


def test_effective_child_recall_inline_forced() -> None:
    # 显式 inline：无论 child 数多少都 inline（child_count 远超 threshold 也不切）
    entry = _make_entry("inline", frozenset(f"c{i}" for i in range(100)))
    assert effective_child_recall(entry, child_count=100, threshold=5) == "inline"


def test_effective_child_recall_deferred_forced() -> None:
    # 显式 deferred：即便 child 数远低于 threshold 也走 deferred
    entry = _make_entry("deferred", frozenset({"c0"}))
    assert effective_child_recall(entry, child_count=1, threshold=50) == "deferred"


def test_effective_child_recall_auto_below_threshold_is_inline() -> None:
    # auto + child_count <= threshold → inline（边界等于阈值也 inline，> 才切）
    entry = _make_entry("auto", frozenset({"c0"}))
    assert effective_child_recall(entry, child_count=5, threshold=5) == "inline"
    assert effective_child_recall(entry, child_count=3, threshold=5) == "inline"


def test_effective_child_recall_auto_above_threshold_is_deferred() -> None:
    # auto + child_count > threshold → deferred
    entry = _make_entry("auto", frozenset({"c0"}))
    assert effective_child_recall(entry, child_count=6, threshold=5) == "deferred"


# ============================================================================
# T6 prompt 文本：inline（含 child 列表）vs deferred（含 search 提示、不列 child）
# ============================================================================


def test_render_prompt_inline_lists_children_when_below_threshold(
    tmp_path: Path,
) -> None:
    """auto entry + 可见 child 数 <= threshold → prompt 含逐条 child 列表。

    且必须与「改 helper 前」逐字一致：``- `id`: desc`` 形式。
    """
    from taifeng.loop.prompt import render_system_prompt
    from taifeng.skill.registry import FilesystemSkillRegistry

    import anyio

    reg = anyio.run(
        lambda: FilesystemSkillRegistry.load(_write_skills(tmp_path))
    )
    snap = reg.snapshot()
    entry = reg.get("orchestrator")
    assert entry is not None

    # 默认 threshold=50，orchestrator 仅 3 个 child → inline
    prompt = render_system_prompt(entry, snap)
    # inline 标志文案 + 逐条 child（与改 helper 前一致）
    assert "You can invoke these child skills via" in prompt
    assert "- `needs-jq`: 需要 jq" in prompt
    assert "- `plain`: 普通" in prompt
    # 不出现 deferred 提示
    assert "search_skills" not in prompt


def test_render_prompt_deferred_hides_children_with_search_hint(
    tmp_path: Path,
) -> None:
    """auto entry + 可见 child 数 > threshold → prompt 不列 child，含 search 提示。"""
    from taifeng.loop.prompt import render_system_prompt
    from taifeng.skill.registry import FilesystemSkillRegistry

    import anyio

    reg = anyio.run(
        lambda: FilesystemSkillRegistry.load(_write_skills(tmp_path))
    )
    snap = reg.snapshot()
    entry = reg.get("orchestrator")
    assert entry is not None

    # 把 threshold 压到 1：可见 child（needs-jq / plain，hidden 被 G4b 过滤）= 2 > 1
    prompt = render_system_prompt(entry, snap, recall_threshold=1)
    # 不逐条列 child
    assert "- `needs-jq`" not in prompt
    assert "- `plain`" not in prompt
    # 含 search_skills 召回提示
    assert "search_skills" in prompt
    # N=过滤后可见 child 数（2）
    assert "共 2 个" in prompt


def test_render_prompt_deferred_override_small_entry(tmp_path: Path) -> None:
    """frontmatter child_recall: deferred 的小 entry（child < threshold）也走 deferred。"""
    from taifeng.loop.prompt import render_system_prompt
    from taifeng.skill.registry import FilesystemSkillRegistry

    import anyio

    _DEFERRED_ENTRY = """---
name: deferred-entry
description: 显式 deferred
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [plain]
exposure:
  child_recall: deferred
---
# deferred entry
"""
    skills = tmp_path / "skills"
    for name, content in (
        ("deferred-entry", _DEFERRED_ENTRY),
        ("plain", _PLAIN),
    ):
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")

    reg = anyio.run(lambda: FilesystemSkillRegistry.load(skills))
    snap = reg.snapshot()
    entry = reg.get("deferred-entry")
    assert entry is not None

    # 仅 1 个 child，远低于默认 threshold=50，但显式 deferred → 仍走 deferred
    prompt = render_system_prompt(entry, snap)
    assert "- `plain`" not in prompt
    assert "search_skills" in prompt
    assert "共 1 个" in prompt
