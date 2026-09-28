"""read_skill —— 正文 + skill 目录内附属文件（渐进加载第三层）。

回归点：read_skill 只能取 SKILL.md 正文，正文中引用的 references/ 等附属文件无从读取。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.builtins.read_skill import MAX_SKILL_FILE_BYTES, make_read_skill_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.tool.spec import ToolResult

_SKILL_MD = "---\nname: guide\ndescription: 指南\n---\n# 指南\n细节见 references/api.md\n"


async def _setup(tmp_path: Path) -> dict[str, Any]:
    """建一个带附属文件的 skill，返回 tool extras。"""
    skill = tmp_path / "skills" / "guide"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (skill / "references" / "api.md").write_text("# API\nGET /items", encoding="utf-8")
    (skill / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    registry = await FilesystemSkillRegistry.load(tmp_path / "skills")
    return {"skill_snapshot": registry.snapshot(), "visible_skills": frozenset({"guide"})}


async def _call(extras: dict[str, Any], **args: Any) -> ToolResult:
    tool = make_read_skill_tool()
    ctx = ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t", extras=extras)
    return await tool.handler(args, ctx)


async def test_without_path_returns_body(tmp_path: Path) -> None:
    result = await _call(await _setup(tmp_path), skill_id="guide")
    assert not result.is_error and "细节见 references/api.md" in result.output


async def test_path_reads_auxiliary_file(tmp_path: Path) -> None:
    """正文引用的附属文件可按相对路径读取。"""
    result = await _call(await _setup(tmp_path), skill_id="guide", path="references/api.md")
    assert not result.is_error
    assert result.output == "# API\nGET /items"
    assert result.data["path"] == "references/api.md"


@pytest.mark.parametrize(("path", "reason"), [
    ("../../secret.txt", "not_visible"),
    ("references/../../../secret.txt", "not_visible"),
    ("missing.md", "not_found"),
    ("references", "not_found"),
    ("blob.bin", "not_text"),
], ids=["dotdot", "nested-dotdot", "missing", "directory", "binary"])
async def test_bad_paths_rejected(tmp_path: Path, path: str, reason: str) -> None:
    result = await _call(await _setup(tmp_path), skill_id="guide", path=path)
    assert result.is_error and result.data["reason"] == reason


async def test_absolute_path_rejected(tmp_path: Path) -> None:
    extras = await _setup(tmp_path)
    result = await _call(extras, skill_id="guide", path=str(tmp_path / "secret.txt"))
    assert result.is_error and result.data["reason"] == "bad_args"


async def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """指向 skill 目录外的符号链接视同越界。"""
    extras = await _setup(tmp_path)
    (tmp_path / "skills" / "guide" / "link.md").symlink_to(tmp_path / "secret.txt")
    result = await _call(extras, skill_id="guide", path="link.md")
    assert result.is_error and result.data["reason"] == "not_visible"


async def test_oversized_file_rejected(tmp_path: Path) -> None:
    extras = await _setup(tmp_path)
    big = tmp_path / "skills" / "guide" / "big.md"
    big.write_text("x" * (MAX_SKILL_FILE_BYTES + 1), encoding="utf-8")
    result = await _call(extras, skill_id="guide", path="big.md")
    assert result.is_error and result.data["reason"] == "too_large"


async def test_invisible_skill_files_not_readable(tmp_path: Path) -> None:
    """可见性与读正文同一规则：不可达的 skill 连附属文件也读不到。"""
    extras = await _setup(tmp_path)
    extras["visible_skills"] = frozenset()
    result = await _call(extras, skill_id="guide", path="references/api.md")
    assert result.is_error and result.data["reason"] == "not_visible"
