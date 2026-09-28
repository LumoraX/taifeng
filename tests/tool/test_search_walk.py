"""search_walk 单测：glob 编译 / 花括号展开 / 逐段匹配 / 沙盒遍历 / 工作线程取消。"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins.search_walk import (
    SearchStopped,
    WalkStats,
    compile_glob,
    expand_braces,
    iter_files,
    rel_to_root,
    run_in_worker,
)
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def _parts(path: str) -> tuple[str, ...]:
    """把 POSIX 相对路径切成段。"""
    return tuple(path.split("/"))


# ── glob 编译与匹配 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.py", "a.py", True),
        ("*.py", "src/a.py", False),          # 标准 glob：* 不跨目录
        ("**/*.py", "a.py", True),            # ** 可吞零段
        ("**/*.py", "src/pkg/a.py", True),
        ("src/**/test_*.py", "src/test_x.py", True),
        ("src/**/test_*.py", "src/a/b/test_x.py", True),
        ("src/**/test_*.py", "lib/test_x.py", False),
        ("src/**", "src/a/b.txt", True),      # 尾随 ** 吞任意多段
        ("a?.txt", "ab.txt", True),
        ("[ab].txt", "c.txt", False),
        ("**/*.{ts,tsx}", "web/app.tsx", True),
        ("**/*.{ts,tsx}", "web/app.js", False),
        ("*.PY", "a.py", False),              # 大小写敏感
        ("./src/*.py", "src/a.py", True),     # . 段与空段被规整掉
        ("**/**/*.md", "docs/a.md", True),    # 连续 ** 不指数回溯
    ],
)
def test_compile_glob_matches(pattern: str, path: str, expected: bool) -> None:
    """逐段匹配语义覆盖常见 LLM 写法。"""
    assert compile_glob(pattern).matches(_parts(path)) is expected


def test_compile_glob_basename_mode_for_include() -> None:
    """include 语义：不含 / 时只比对文件名（任意深度）；含 / 时比对相对路径。"""
    by_name = compile_glob("*.py", basename_if_no_slash=True)
    assert by_name.matches(_parts("deep/pkg/a.py"))
    by_path = compile_glob("src/*.py", basename_if_no_slash=True)
    assert by_path.matches(_parts("src/a.py"))
    assert not by_path.matches(_parts("lib/src/a.py"))


@pytest.mark.parametrize(
    ("pattern", "fragment"),
    [
        ("", "non-empty"),
        ("   ", "non-empty"),
        ("/etc/*", "relative"),
        ("../*.py", ".."),
        ("src/../../x", ".."),
        ("*.{py", "unbalanced"),
        ("x" * 1001, "longer than"),
        ("{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}", "brace expansion"),
    ],
)
def test_compile_glob_rejects_bad_patterns(pattern: str, fragment: str) -> None:
    """空 / 绝对 / .. 逃逸 / 花括号不配对 / 超长 / 展开爆炸均拒绝。"""
    with pytest.raises(ValueError, match=fragment):
        compile_glob(pattern)


def test_expand_braces_nested_and_literal() -> None:
    """嵌套花括号逐层展开；无逗号的 {x} 等价于 x；无花括号原样返回。"""
    assert sorted(expand_braces("a.{py,{ts,tsx}}")) == ["a.py", "a.ts", "a.tsx"]
    assert expand_braces("{x}.md") == ["x.md"]
    assert expand_braces("plain/*.txt") == ["plain/*.txt"]


# ── 沙盒遍历 ────────────────────────────────────────────────────────────────


def _walk(base: Path, root: Path, exclude: frozenset[str] = frozenset()) -> list[str]:
    """跑一次完整遍历，返回相对 root 的路径列表。"""
    stats = WalkStats()
    return [
        rel_to_root(p, root)
        for p in iter_files(
            base, root=root, exclude_dirs=exclude, should_stop=lambda: False, stats=stats,
        )
    ]


def test_iter_files_order_is_path_segment_lexicographic(tmp_path: Path) -> None:
    """深度优先 + 同层按名排序 = 按路径逐段字典序（a/z.py 先于 a.py）。"""
    (tmp_path / "a" / "b").mkdir(parents=True)
    for rel in ("b.py", "a.py", "a/z.py", "a/b/c.py"):
        (tmp_path / rel).write_text("x", encoding="utf-8")
    got = _walk(tmp_path, tmp_path)
    assert got == ["a/b/c.py", "a/z.py", "a.py", "b.py"]
    assert got == sorted(got, key=lambda p: p.split("/"))


def test_iter_files_skips_excluded_dirs_but_not_explicit_base(tmp_path: Path) -> None:
    """排除目录不下探；但显式把它当基点时照常遍历。"""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "src.py").write_text("x", encoding="utf-8")
    exclude = frozenset({".git"})
    assert _walk(tmp_path, tmp_path, exclude) == ["src.py"]
    assert _walk(tmp_path / ".git", tmp_path, exclude) == [".git/config"]


def test_iter_files_symlink_policy(tmp_path: Path) -> None:
    """指向 root 外 / 指向目录 / 悬空的链接跳过计数；指向 root 内文件的链接保留。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    (root / "real.txt").write_text("r", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "inside_link.txt").symlink_to(root / "real.txt")
    (root / "escape_file.txt").symlink_to(outside / "secret.txt")
    (root / "escape_dir").symlink_to(outside)
    (root / "loop_dir").symlink_to(root / "sub")
    (root / "dangling.txt").symlink_to(root / "missing.txt")
    stats = WalkStats()
    got = [
        rel_to_root(p, root)
        for p in iter_files(
            root, root=root, exclude_dirs=frozenset(), should_stop=lambda: False, stats=stats,
        )
    ]
    assert got == ["inside_link.txt", "real.txt"]
    assert stats.skipped_symlinks == 4


def test_iter_files_stop_signal_raises(tmp_path: Path) -> None:
    """停止信号在目录项检查点抛 SearchStopped。"""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SearchStopped):
        list(iter_files(
            tmp_path, root=tmp_path, exclude_dirs=frozenset(),
            should_stop=lambda: True, stats=WalkStats(),
        ))


# ── 工作线程执行与取消（R4）───────────────────────────────────────────────────


def _blocking_fn(started: threading.Event, exited: threading.Event):
    """构造一个「一直干活直到被叫停」的阻塞函数（模拟超大目录遍历）。"""

    def fn(should_stop) -> None:  # noqa: ANN001 —— 测试替身
        started.set()
        try:
            while not should_stop():
                time.sleep(0.001)
            raise SearchStopped
        finally:
            exited.set()

    return fn


async def test_run_in_worker_token_cancel_stops_thread() -> None:
    """遍历进行中 token 取消 → 线程在下一个检查点退出并抛 SearchStopped。"""
    token = CancellationToken()
    started, exited = threading.Event(), threading.Event()
    task = asyncio.create_task(run_in_worker(_blocking_fn(started, exited), token))
    await wait_for_condition(started.is_set)
    token.cancel()
    with pytest.raises(SearchStopped):
        await task
    assert exited.is_set()


async def test_run_in_worker_abandoned_await_stops_thread() -> None:
    """工具超时（await 被取消）→ await 立即返回，被放弃的线程随即自行退出不空转。"""
    token = CancellationToken()
    started, exited = threading.Event(), threading.Event()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_in_worker(_blocking_fn(started, exited), token), 0.05)
    await wait_for_condition(exited.is_set, message="被放弃的工作线程未退出")
    assert not token.is_cancelled  # 超时不改写业务 token


async def test_run_in_worker_returns_value() -> None:
    """正常路径：透传返回值。"""
    assert await run_in_worker(lambda stop: 42, CancellationToken()) == 42
