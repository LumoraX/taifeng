"""grep 扫描测试（ADR 0071）：上下文行（合并 / 分隔 / 名额 / 截断 / 参数校验）与
跨行匹配（起止行号 / 片段 / 三种输出模式 / 截断 / CRLF / 停止信号）。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins import make_grep_tool
from taifeng.tool.builtins.grep_scan import GrepLimits, GrepQuery, GrepRun
from taifeng.tool.builtins.search_walk import SearchStopped
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx() -> ToolContext:
    """最小工具上下文。"""
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


def _write(root: Path, rel: str, text: str) -> None:
    """在 root 下写文本文件（自动建父目录，按字节写入以保留 CRLF）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _numbered(hits: set[int], total: int) -> str:
    """生成 total 行文本：hits 中的行号写 ``hit N``，其余写 ``l N``。"""
    return "\n".join(f"hit {n}" if n in hits else f"l {n}" for n in range(1, total + 1)) + "\n"


async def _grep(root: Path, args: dict[str, Any], **factory: Any) -> Any:
    """以给定工厂参数构造 grep 并调用一次。"""
    return await make_grep_tool(root_dir=root, **factory).handler(args, _ctx())


# ── 上下文行 ────────────────────────────────────────────────────────────────


async def test_context_merges_overlap_and_separates_groups(tmp_path: Path) -> None:
    """重叠区间合并不重复；不相邻组之间、跨文件之间以 -- 分隔；上下文行用 - 连接。"""
    _write(tmp_path, "a.txt", _numbered({3, 5, 9}, 10))
    _write(tmp_path, "b.txt", "hit b\nl 2\n")
    r = await _grep(tmp_path, {"pattern": "hit", "context": 1})
    assert not r.is_error
    assert r.output.splitlines() == [
        "a.txt-2-l 2", "a.txt:3:hit 3", "a.txt-4-l 4", "a.txt:5:hit 5", "a.txt-6-l 6",
        "--",
        "a.txt-8-l 8", "a.txt:9:hit 9", "a.txt-10-l 10",
        "--",
        "b.txt:1:hit b", "b.txt-2-l 2",
    ]
    assert r.data["count"] == 4
    assert r.data["context_lines"] == 6
    assert r.data["truncated"] is False


async def test_context_adjacent_groups_are_contiguous(tmp_path: Path) -> None:
    """前一组后文与后一组前文首尾相接时并为一组，不插 --。"""
    _write(tmp_path, "a.txt", _numbered({3, 6}, 8))
    r = await _grep(tmp_path, {"pattern": "hit", "context": 1})
    assert r.output.splitlines() == [
        "a.txt-2-l 2", "a.txt:3:hit 3", "a.txt-4-l 4",
        "a.txt-5-l 5", "a.txt:6:hit 6", "a.txt-7-l 7",
    ]


async def test_context_one_side_and_override(tmp_path: Path) -> None:
    """单侧参数优先于 context；只要前文 / 只要后文均可。"""
    _write(tmp_path, "a.txt", _numbered({3}, 5))
    before = await _grep(tmp_path, {"pattern": "hit", "context_before": 2})
    assert before.output.splitlines() == ["a.txt-1-l 1", "a.txt-2-l 2", "a.txt:3:hit 3"]
    after = await _grep(tmp_path, {"pattern": "hit", "context": 2, "context_before": 0})
    assert after.output.splitlines() == ["a.txt:3:hit 3", "a.txt-4-l 4", "a.txt-5-l 5"]


async def test_context_lines_are_clipped(tmp_path: Path) -> None:
    """上下文行同样受 max_line_chars 约束。"""
    _write(tmp_path, "a.txt", "y" * 30 + "\nhit\n")
    r = await _grep(tmp_path, {"pattern": "hit", "context_before": 1}, max_line_chars=5)
    assert r.output.splitlines()[0] == "a.txt-1-yyyyy …[line truncated, 30 chars]"


async def test_context_counts_toward_max_results(tmp_path: Path) -> None:
    """上下文行占名额；后文放不下时截断并告知（单位是输出行）。"""
    _write(tmp_path, "a.txt", _numbered({2, 6, 10}, 12))
    r = await _grep(tmp_path, {"pattern": "hit", "context": 1}, max_results=5)
    lines = r.output.splitlines()
    assert lines[:6] == [
        "a.txt-1-l 1", "a.txt:2:hit 2", "a.txt-3-l 3", "--", "a.txt-5-l 5", "a.txt:6:hit 6",
    ]
    assert "results truncated at 5 output lines (matches + context)" in lines[6]
    assert r.data["truncated"] is True
    assert (r.data["count"], r.data["context_lines"]) == (2, 3)


async def test_context_group_that_does_not_fit_is_not_emitted(tmp_path: Path) -> None:
    """一组（前文 + 匹配行）放不下时整组不输出，不留只有前文的残组。"""
    _write(tmp_path, "a.txt", _numbered({1, 10}, 10))
    r = await _grep(tmp_path, {"pattern": "hit", "context_before": 2}, max_results=3)
    lines = r.output.splitlines()
    assert lines[0] == "a.txt:1:hit 1"
    assert "results truncated at 3" in lines[1]
    assert len(lines) == 2


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"context": 1, "output_mode": "count"}, "only apply to output_mode=content"),
        ({"context_after": 1, "output_mode": "files_with_matches"}, "only apply"),
        ({"context": 1, "multiline": True}, "not supported with multiline"),
        ({"context": -1}, "context must be an integer >= 0"),
        ({"context_before": 1.5}, "context_before must be an integer >= 0"),
        ({"context_after": True}, "context_after must be an integer >= 0"),
        ({"context_before": 2, "context_after": 1}, "must be < max_results (3)"),
        ({"multiline": "yes"}, "multiline must be boolean"),
    ],
)
async def test_context_and_multiline_bad_args(
    tmp_path: Path, args: dict[str, Any], fragment: str,
) -> None:
    """非法 / 无意义的组合以 bad_args 明确拒绝，不静默忽略。"""
    _write(tmp_path, "a.txt", "hit\n")
    r = await _grep(tmp_path, {"pattern": "hit", **args}, max_results=3)
    assert r.is_error
    assert r.data["reason"] == "bad_args"
    assert fragment in r.output


async def test_zero_context_is_allowed_in_any_mode(tmp_path: Path) -> None:
    """context=0 等于不带上下文，其他模式下也合法。"""
    _write(tmp_path, "a.txt", "hit\nhit\n")
    r = await _grep(tmp_path, {"pattern": "hit", "context": 0, "output_mode": "count"})
    assert r.output == "a.txt:2"


# ── 跨行匹配 ────────────────────────────────────────────────────────────────


_SRC = "def f(\n    x,\n):\n    return x\n\nclass A:\n    pass\n"


async def test_multiline_reports_line_span_and_snippet(tmp_path: Path) -> None:
    """跨行匹配输出 起始行-结束行 与 JSON 片段；DOTALL 让 . 跨行。"""
    _write(tmp_path, "m.py", _SRC)
    r = await _grep(tmp_path, {"pattern": r"def f\(.*?\):", "multiline": True})
    assert not r.is_error
    assert r.output == 'm.py:1-3:"def f(\\n    x,\\n):"'
    assert r.data["multiline"] is True
    assert r.data["count"] == 1


async def test_multiline_anchors_match_each_line(tmp_path: Path) -> None:
    """MULTILINE：^ / $ 匹配每行首尾；逐行模式同一模式找不到跨行片段。"""
    _write(tmp_path, "m.py", _SRC)
    r = await _grep(tmp_path, {"pattern": r"^class \w+:$\n^\s+pass$", "multiline": True})
    assert r.output == 'm.py:6-7:"class A:\\n    pass"'
    plain = await _grep(tmp_path, {"pattern": r"x,\n\)"})
    assert plain.output == "no matches found"


async def test_multiline_trailing_newline_stays_on_its_line(tmp_path: Path) -> None:
    """匹配以换行结尾时，结束行是该换行所在行（不算下一行）。"""
    _write(tmp_path, "m.py", "a\nb\nc\n")
    r = await _grep(tmp_path, {"pattern": r"b\n", "multiline": True})
    assert r.output == 'm.py:2-2:"b\\n"'


async def test_multiline_count_files_and_ignore_case(tmp_path: Path) -> None:
    """count 计匹配数；files_with_matches 列文件；ignore_case 与 multiline 叠加。"""
    _write(tmp_path, "a.txt", "Foo\nbar\nfoo\nbar\n")
    _write(tmp_path, "b.txt", "nothing\n")
    count = await _grep(
        tmp_path,
        {"pattern": r"foo\nbar", "multiline": True, "ignore_case": True, "output_mode": "count"},
    )
    assert count.output == "a.txt:2"
    files = await _grep(
        tmp_path, {"pattern": r"o\nb", "multiline": True, "output_mode": "files_with_matches"},
    )
    assert files.output == "a.txt"


async def test_multiline_crlf_is_normalized(tmp_path: Path) -> None:
    """CRLF 规整为 LF 后 $ 落在行尾；行号与 file_read 同口径。"""
    _write(tmp_path, "w.txt", "one\r\ntwo\r\nthree\r\n")
    r = await _grep(tmp_path, {"pattern": r"two$\nthree", "multiline": True})
    assert r.output == 'w.txt:2-3:"two\\nthree"'


async def test_multiline_truncation_and_snippet_cap(tmp_path: Path) -> None:
    """匹配数超上限截断（单位 matches）；片段超长截断并注明原长度。"""
    _write(tmp_path, "a.txt", "ab\n" * 4)
    r = await _grep(tmp_path, {"pattern": r"a.", "multiline": True}, max_results=2)
    lines = r.output.splitlines()
    assert lines[:2] == ['a.txt:1-1:"ab"', 'a.txt:2-2:"ab"']
    assert "results truncated at 2 matches" in lines[2]
    long = await _grep(
        tmp_path, {"pattern": r"(ab\n)+", "multiline": True}, max_line_chars=4,
    )
    assert long.output == 'a.txt:1-4:"ab\\na" …[match truncated, 12 chars]'


async def test_multiline_empty_match_line_number(tmp_path: Path) -> None:
    """空匹配（如 ^$ 命中空行）按起点定位行号。"""
    _write(tmp_path, "a.txt", "x\n\ny\n")
    r = await _grep(tmp_path, {"pattern": r"^$", "multiline": True})
    assert r.output.splitlines()[0] == 'a.txt:2-2:""'


def test_multiline_scan_honours_stop_signal(tmp_path: Path) -> None:
    """跨行模式按批检查停止信号：大量匹配时停止信号生效抛 SearchStopped。"""
    _write(tmp_path, "a.txt", "x" * 1000)
    run = GrepRun(
        root=tmp_path.resolve(),
        query=GrepQuery(
            regex=re.compile("x", re.MULTILINE | re.DOTALL), include=None, mode="count",
            path=".", multiline=True,
        ),
        limits=GrepLimits(max_results=10, max_line_chars=100, max_file_bytes=1 << 20),
        exclude_dirs=frozenset(), gitignore=False,
    )
    with pytest.raises(SearchStopped):
        run.run(tmp_path.resolve(), lambda: True)
