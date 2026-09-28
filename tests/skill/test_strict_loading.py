"""strict-skill-loading —— SKILL.md 出错时加载期显式失败，不静默跳过 / 截断 / 误读。

回归点：
- frontmatter 缺失或 YAML 错误的 SKILL.md 只记 warning 后被跳过，skill 从列表里悄悄消失；
- 超长 body 被截断，模型拿到残缺指令；
- ``entry: "false"`` 经 ``bool()`` 读成 True，``child_skills: x``（漏方括号）被拆成字符集合；
- 多目录同名 skill 静默覆盖。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from taifeng.skill.definition import SkillValidationError
from taifeng.skill.loader import MAX_SKILL_BODY_SIZE, load_skills_from_dir
from taifeng.skill.registry import FilesystemSkillRegistry

if TYPE_CHECKING:
    from pathlib import Path

_BASE = """name: s
description: 一个 skill
type: composite
tool_names: [file_read]
"""


def _write(root: Path, text: str, skill_id: str = "s") -> Path:
    (root / skill_id).mkdir(parents=True, exist_ok=True)
    (root / skill_id / "SKILL.md").write_text(text, encoding="utf-8")
    return root


def _with(extra: str) -> str:
    return f"---\n{_BASE}{extra}\n---\n# body\n"


@pytest.mark.parametrize(("text", "match"), [
    ("# 没有 frontmatter\n", "frontmatter"),
    ("---\nname: [unclosed\n---\nbody\n", "YAML"),
    ("---\n- a\n- b\n---\nbody\n", "mapping"),
], ids=["no-frontmatter", "bad-yaml", "list-frontmatter"])
def test_broken_skill_md_fails_instead_of_disappearing(
    tmp_path: Path, text: str, match: str,
) -> None:
    """SKILL.md 存在但无法解析 → 加载失败（此前 warning 后跳过）。"""
    with pytest.raises(SkillValidationError, match=match):
        load_skills_from_dir(_write(tmp_path, text))


def test_directory_without_skill_md_is_still_skipped(tmp_path: Path) -> None:
    """没有 SKILL.md 的子目录（如共享素材）仍合法跳过。"""
    (tmp_path / "_shared").mkdir()
    skills = load_skills_from_dir(_write(tmp_path, _with("")))
    assert set(skills) == {"s"}


def test_oversized_body_fails_instead_of_truncating(tmp_path: Path) -> None:
    """body 超过上限 → 加载失败（此前截断后带着残缺指令运行）。"""
    text = f"---\n{_BASE}---\n" + "字" * (MAX_SKILL_BODY_SIZE // 3 + 1)
    with pytest.raises(SkillValidationError, match="超过上限"):
        load_skills_from_dir(_write(tmp_path, text))


@pytest.mark.parametrize(("extra", "key"), [
    ('entry: "false"', "entry"),
    ("child_skills: other", "child_skills"),
    ("tool_names: file_read", "tool_names"),
    ("tool_names: [1, 2]", "tool_names"),
    ("max_call_depth: true", "max_call_depth"),
    ("max_call_depth: 0", "max_call_depth"),
    ("max_call_depth: 2.5", "max_call_depth"),
    ("model: 42", "model"),
    ("requires: {bins: jq}", "bins"),
    ('exposure: {model_invocable: "no"}', "model_invocable"),
    ("exposure: [a]", "exposure"),
], ids=["entry-str", "child-bare", "tools-bare", "tools-ints", "depth-bool", "depth-zero",
        "depth-float", "model-int", "bins-bare", "invocable-str", "exposure-list"])
def test_wrongly_typed_field_rejected(tmp_path: Path, extra: str, key: str) -> None:
    """已知字段类型不符 → 报错，不做 bool() / frozenset() 强转。"""
    with pytest.raises(SkillValidationError, match=key):
        load_skills_from_dir(_write(tmp_path, _with(extra)))


@pytest.mark.parametrize("fields", ["name: s\ndescription:\n", 'name: ""\ndescription: d\n'],
                         ids=["null-description", "empty-name"])
def test_empty_required_field_rejected(tmp_path: Path, fields: str) -> None:
    """必填字段为 null / 空串视同缺失。"""
    text = f"---\n{fields}type: composite\ntool_names: [x]\n---\nbody\n"
    with pytest.raises(SkillValidationError, match="不能为空"):
        load_skills_from_dir(_write(tmp_path, text))


def test_unknown_top_level_keys_pass_through(tmp_path: Path) -> None:
    """顶层未知键保留给业务透传（frontmatter_raw），不报错。"""
    skills = load_skills_from_dir(_write(tmp_path, _with("owner_team: platform")))
    assert skills["s"].frontmatter_raw["owner_team"] == "platform"


async def test_cross_directory_override_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """多目录同名 skill：靠后目录覆盖靠前目录，并告警写明两处路径。"""
    first = _write(tmp_path / "a", _with(""))
    second = _write(tmp_path / "b", _with("").replace("# body", "# 覆盖版"))
    with caplog.at_level(logging.WARNING, logger="taifeng.skill.registry"):
        registry = await FilesystemSkillRegistry.load([first, second])
    (skill,) = registry.snapshot().skills
    assert "覆盖版" in skill.body
    assert any("overrides" in r.getMessage() and str(first) in r.getMessage()
               for r in caplog.records)
