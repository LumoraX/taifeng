"""gitignore 规则解析 / 匹配 / 遍历集成测试（ADR 0071）：常见语法、否定与层级优先序、
不支持语法的显式计数、.gitignore 读取边界，以及 glob / grep 工具的开关与尾注。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins import make_glob_tool, make_grep_tool
from taifeng.tool.builtins.gitignore import (
    MAX_GITIGNORE_BYTES,
    IgnoreLevel,
    UnsupportedIgnorePattern,
    compile_rule,
    is_ignored,
    parse_gitignore,
    read_gitignore,
)
from taifeng.tool.builtins.search_walk import WalkStats, iter_files, rel_to_root
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx() -> ToolContext:
    """最小工具上下文。"""
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    """在 root 下写文本文件（自动建父目录）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ignored(lines: str, path: str, *, is_dir: bool = False) -> bool:
    """用单层（根目录）规则判定相对路径是否被忽略。"""
    rules, unsupported = parse_gitignore(lines)
    assert unsupported == 0
    return is_ignored([IgnoreLevel(base_parts=(), rules=rules)], tuple(path.split("/")),
                      is_dir=is_dir)


# ── 规则语法 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        ("*.log", "a.log", False, True),
        ("*.log", "deep/sub/a.log", False, True),      # 不含 / → 任意深度按名称
        ("*.log", "a.log.txt", False, False),
        ("/build", "build", True, True),              # 开头 / 锚定
        ("/build", "src/build", True, False),
        ("build/", "build", True, True),              # 尾斜杠只匹配目录
        ("build/", "build", False, False),
        ("build/", "src/build", True, True),          # 尾斜杠不算锚定
        ("doc/*.txt", "doc/a.txt", False, True),      # 中间 / 锚定
        ("doc/*.txt", "doc/sub/a.txt", False, False),  # * 不跨目录
        ("doc/*.txt", "x/doc/a.txt", False, False),
        ("**/foo", "foo", False, True),               # 开头 **
        ("**/foo", "a/b/foo", True, True),
        ("foo/**", "foo/a", False, True),             # 结尾 **
        ("foo/**", "foo/a/b", False, True),
        ("foo/**", "foo", True, False),
        ("a/**/b", "a/b", False, True),               # 中间 ** = 零或多层
        ("a/**/b", "a/x/y/b", False, True),
        ("a/**/b", "a/x/c", False, False),
        ("?.md", "a.md", False, True),
        ("?.md", "ab.md", False, False),
        ("[abc].txt", "b.txt", False, True),
        ("[abc].txt", "d.txt", False, False),
        ("[!abc].txt", "d.txt", False, True),         # [!..] 取反
        ("[^abc].txt", "a.txt", False, False),
        ("[a-c]x", "bx", False, True),                # 区间
        ("\\#hash", "#hash", False, True),             # \# 转义为字面量
        ("\\!bang", "!bang", False, True),             # \! 转义为字面量
        ("trail   ", "trail", False, True),           # 行尾未转义空格去掉
        ("space\\ ", "space ", False, True),           # \  保留
        ("a*b", "a/b", False, False),                 # * 不匹配 /
    ],
)
def test_rule_syntax(pattern: str, path: str, is_dir: bool, expected: bool) -> None:
    """常见 gitignore 语法逐条覆盖。"""
    assert _ignored(pattern, path, is_dir=is_dir) is expected


def test_comments_blank_and_bom_crlf() -> None:
    """注释 / 空行不成规则；BOM 与 CRLF 不影响解析。"""
    rules, unsupported = parse_gitignore("﻿# comment\r\n\r\n*.tmp\r\n!\r\n/\r\n")
    assert unsupported == 0
    assert len(rules) == 1
    assert compile_rule("# only comment") is None
    assert compile_rule("   ") is None


def test_negation_last_match_wins() -> None:
    """同一文件内最后一条命中的规则说了算。"""
    assert _ignored("*.log\n!keep.log", "keep.log") is False
    assert _ignored("*.log\n!keep.log", "x.log") is True
    assert _ignored("!keep.log\n*.log", "keep.log") is True


def test_deeper_level_overrides_parent() -> None:
    """子目录 .gitignore 优先于父目录。"""
    root_rules, _ = parse_gitignore("*.txt")
    sub_rules, _ = parse_gitignore("!keep.txt")
    levels = [IgnoreLevel((), root_rules), IgnoreLevel(("sub",), sub_rules)]
    assert is_ignored(levels, ("sub", "keep.txt"), is_dir=False) is False
    assert is_ignored(levels, ("sub", "other.txt"), is_dir=False) is True
    assert is_ignored(levels[:1], ("keep.txt",), is_dir=False) is True


@pytest.mark.parametrize("line", ["[[:alpha:]].txt", "[abc", "foo\\"])
def test_unsupported_syntax_is_reported(line: str) -> None:
    """POSIX 字符类 / 未闭合 [ / 行尾孤立反斜杠：抛不支持，解析时计数而非静默。"""
    with pytest.raises(UnsupportedIgnorePattern):
        compile_rule(line)
    rules, unsupported = parse_gitignore(f"{line}\n*.ok")
    assert (len(rules), unsupported) == (1, 1)


def test_read_gitignore_boundaries(tmp_path: Path) -> None:
    """不存在 → None；符号链接不读；超大 / 非 UTF-8 → OSError（调用方计入 unreadable）。"""
    assert read_gitignore(tmp_path) is None
    target = tmp_path / "real"
    _write(target, ".gitignore", "*.x\n")
    link_dir = tmp_path / "linked"
    link_dir.mkdir()
    os.symlink(target / ".gitignore", link_dir / ".gitignore")
    assert read_gitignore(link_dir) is None
    big = tmp_path / "big"
    big.mkdir()
    (big / ".gitignore").write_bytes(b"#" * (MAX_GITIGNORE_BYTES + 1))
    with pytest.raises(OSError, match="larger than"):
        read_gitignore(big)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".gitignore").write_bytes(b"\xff\xfe*.x\n")
    with pytest.raises(OSError, match="UTF-8"):
        read_gitignore(bad)


# ── 遍历集成 ────────────────────────────────────────────────────────────────


def _walk(
    root: Path, base: Path | None = None, *, gitignore: bool = True,
) -> tuple[list[str], WalkStats]:
    """以 gitignore 开关遍历，返回 (相对根路径列表, 统计)。"""
    stats = WalkStats()
    resolved = root.resolve()
    files = iter_files(
        (base or root).resolve(), root=resolved, exclude_dirs=frozenset({".git"}),
        should_stop=lambda: False, stats=stats, gitignore=gitignore,
    )
    return [rel_to_root(p, resolved) for p in files], stats


def _tree(root: Path) -> None:
    """根规则 + 子目录规则 + 被忽略目录的典型工作区。"""
    _write(root, ".gitignore", "build/\n*.log\n!keep.log\n")
    for rel in ("build/out.py", "a.log", "keep.log", "src/b.py", "src/c.tmp", "src/sub/c.tmp",
                "src/sub/d.py"):
        _write(root, rel)
    _write(root, "src/sub/.gitignore", "*.tmp\n")


def test_walk_prunes_ignored_paths_and_counts(tmp_path: Path) -> None:
    """被忽略目录不下探、被忽略文件跳过，均计入 skipped_ignored；子目录规则只管其子树。"""
    _tree(tmp_path)
    paths, stats = _walk(tmp_path)
    assert paths == [".gitignore", "keep.log", "src/b.py", "src/c.tmp", "src/sub/.gitignore",
                     "src/sub/d.py"]
    assert stats.skipped_ignored == 3  # build/ 目录 + a.log + src/sub/c.tmp


def test_walk_disabled_keeps_everything(tmp_path: Path) -> None:
    """gitignore=False（iter_files 缺省）不读规则。"""
    _tree(tmp_path)
    paths, stats = _walk(tmp_path, gitignore=False)
    assert "build/out.py" in paths and "a.log" in paths
    assert stats.skipped_ignored == 0


def test_walk_applies_ancestor_rules_from_sandbox_root(tmp_path: Path) -> None:
    """基点在子目录时，沙盒根到基点沿途的 .gitignore 仍然生效。"""
    _tree(tmp_path)
    _write(tmp_path, "src/sub/z.log")
    paths, _ = _walk(tmp_path, tmp_path / "src" / "sub")
    assert paths == ["src/sub/.gitignore", "src/sub/d.py"]


def test_explicit_ignored_base_is_searched(tmp_path: Path) -> None:
    """显式把被忽略的目录作为基点时照常遍历（其下路径仍逐条判定）。"""
    _tree(tmp_path)
    _write(tmp_path, "build/debug.log")
    paths, _ = _walk(tmp_path, tmp_path / "build")
    assert paths == ["build/out.py"]  # debug.log 仍被根目录的 *.log 忽略


def test_cannot_reinclude_under_ignored_parent(tmp_path: Path) -> None:
    """父目录被忽略后，子路径的 ! 规则无效（与 git 一致）。"""
    _write(tmp_path, ".gitignore", "build/\n!build/keep.py\n")
    _write(tmp_path, "build/keep.py")
    paths, stats = _walk(tmp_path)
    assert paths == [".gitignore"]
    assert stats.skipped_ignored == 1


def test_unsupported_and_unreadable_gitignore_are_counted(tmp_path: Path) -> None:
    """不支持的行不生效且计数；读不了的 .gitignore 计入 unreadable。"""
    _write(tmp_path, ".gitignore", "[[:digit:]].txt\n")
    _write(tmp_path, "1.txt")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / ".gitignore").write_bytes(b"\xff\n")
    paths, stats = _walk(tmp_path)
    assert "1.txt" in paths  # 规则未生效 → 未被跳过
    assert stats.gitignore_unsupported == 1
    assert stats.unreadable == 1


# ── 工具开关与尾注 ─────────────────────────────────────────────────────────────


async def test_glob_respects_gitignore_by_default(tmp_path: Path) -> None:
    """glob 默认遵循 .gitignore 并在尾注告知；respect_gitignore=False 列出全部。"""
    _tree(tmp_path)
    r = await make_glob_tool(root_dir=tmp_path).handler({"pattern": "**/*.py"}, _ctx())
    lines = r.output.splitlines()
    assert lines[:2] == ["src/b.py", "src/sub/d.py"]
    assert "skipped 3 path(s) ignored by .gitignore" in lines[2]
    assert r.data["skipped_ignored"] == 3
    raw = await make_glob_tool(root_dir=tmp_path, respect_gitignore=False).handler(
        {"pattern": "**/*.py"}, _ctx(),
    )
    assert raw.output.splitlines() == ["build/out.py", "src/b.py", "src/sub/d.py"]
    assert ".gitignore" not in (make_glob_tool(root_dir=tmp_path, respect_gitignore=False)
                                .description)


async def test_grep_respects_gitignore_and_explicit_base(tmp_path: Path) -> None:
    """grep 默认跳过被忽略路径；把被忽略目录作为 path 可显式搜索；不支持的行在尾注告知。"""
    _tree(tmp_path)
    _write(tmp_path, "build/out.py", "needle\n")
    _write(tmp_path, "src/b.py", "needle\n")
    tool = make_grep_tool(root_dir=tmp_path)
    r = await tool.handler({"pattern": "needle"}, _ctx())
    assert r.output.splitlines()[0] == "src/b.py:1:needle"
    assert r.data["skipped_ignored"] == 3
    explicit = await tool.handler({"pattern": "needle", "path": "build"}, _ctx())
    assert explicit.output == "build/out.py:1:needle"
    _write(tmp_path, ".gitignore", "[[:alpha:]]\n")
    noted = await tool.handler({"pattern": "needle"}, _ctx())
    assert "1 .gitignore line(s) use unsupported syntax" in noted.output
    assert noted.data["gitignore_unsupported"] == 1
