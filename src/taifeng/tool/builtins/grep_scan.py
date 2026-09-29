"""grep 的工作线程扫描：逐行 / 带上下文行 / 跨行（multiline）三种扫描与结果名额记账。

从 ``grep_search`` 拆出（ADR 0071 增加上下文行与跨行匹配后，参数解析 / schema / 渲染与
扫描逻辑分居两个模块，各自保持在行数警戒线内）。

输出行形状（``content`` 模式）：

- 匹配行 ``路径:行号:行``；上下文行 ``路径-行号-行``；不相邻的组之间一行 ``--``
  （与 ``grep -C`` / ``rg -C`` 一致）；重叠或相邻的上下文区间合并为一组，不重复输出；
- 跨行匹配 ``路径:起始行-结束行:"片段"``——片段是 JSON 字符串（换行显示为 ``\\n``，
  无歧义），超过 ``max_line_chars`` 截断并注明原长度。

名额：``max_results`` 限制的是**输出行数**——匹配行与上下文行都占名额（``--`` 不占）；
一组（前文 + 匹配行）放不下时整组不输出并标记截断，不会留下只有前文没有匹配行的残组。
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from taifeng.tool.builtins.search_walk import (
    GlobMatcher,
    SearchStopped,
    WalkStats,
    iter_files,
    rel_to_root,
)

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from pathlib import Path

GrepOutputMode = Literal["content", "files_with_matches", "count"]

#: 二进制嗅探窗口：前 8KB 出现 NUL 即判二进制（git / ripgrep 同款启发式）
_BINARY_SNIFF_BYTES = 8192

#: 大文件内每扫多少行检查一次停止信号（逐行检查开销不划算，按批检查足够及时）
_STOP_CHECK_EVERY_LINES = 1024

#: 跨行模式下每产出多少个匹配检查一次停止信号
_STOP_CHECK_EVERY_MATCHES = 256

#: 不相邻的上下文组之间的分隔行（不占结果名额）
GROUP_SEPARATOR = "--"


@dataclass(frozen=True)
class GrepQuery:
    """一次 grep 调用的已校验参数。

    Attributes:
        regex: 已编译正则（``multiline=True`` 时已带 ``MULTILINE | DOTALL``）。
        include: 文件过滤 glob；None = 不过滤。
        mode: 输出模式。
        path: 搜索基点（相对沙盒根）。
        before: 每个匹配行之前的上下文行数（仅 content、非 multiline）。
        after: 每个匹配行之后的上下文行数（仅 content、非 multiline）。
        multiline: 整文件匹配（允许跨行）。
    """

    regex: re.Pattern[str]
    include: GlobMatcher | None
    mode: GrepOutputMode
    path: str
    before: int = 0
    after: int = 0
    multiline: bool = False

    @property
    def with_context(self) -> bool:
        """是否输出上下文行。"""
        return self.before > 0 or self.after > 0


@dataclass(frozen=True)
class GrepLimits:
    """工厂级上限（构造时确定，调用间不变）。"""

    max_results: int
    max_line_chars: int
    max_file_bytes: int


def _line_starts(text: str) -> list[int]:
    """每行起始偏移（按 ``str.splitlines`` 切行，行号口径与 ``file_read`` 的 offset 一致）。"""
    starts: list[int] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        starts.append(pos)
        pos += len(line)
    return starts or [0]


@dataclass
class GrepRun:
    """工作线程内的一次搜索：遍历、逐文件扫描、按名额收集结果。

    Attributes:
        hits: 输出行（含 ``--`` 分隔行）。
        used: 已占用的结果名额（匹配行 / 匹配 / 文件 + 上下文行；分隔行不占）。
        count: 结果数——content 模式为匹配行数（multiline 为匹配数），其余模式为文件数。
        context_lines: 输出的上下文行数。
    """

    root: Path
    query: GrepQuery
    limits: GrepLimits
    exclude_dirs: frozenset[str]
    gitignore: bool
    hits: list[str] = field(default_factory=list)
    used: int = 0
    count: int = 0
    context_lines: int = 0
    truncated: bool = False
    files_scanned: int = 0
    stats: WalkStats = field(default_factory=WalkStats)

    def run(self, base: Path, should_stop: Callable[[], bool]) -> None:
        """遍历 ``base``（文件或目录）并扫描；结果名额用尽即停。

        Raises:
            SearchStopped: 取消 / 超时的停止信号。
        """
        files = iter_files(
            base, root=self.root, exclude_dirs=self.exclude_dirs,
            should_stop=should_stop, stats=self.stats, gitignore=self.gitignore,
        )
        for path in files:
            # include 过滤：相对搜索基点比对（基点是文件时只剩文件名一段）
            rel_parts = path.relative_to(base).parts or (path.name,)
            if self.query.include is not None and not self.query.include.matches(rel_parts):
                continue
            if self._scan(path, should_stop):
                return

    def _scan(self, path: Path, should_stop: Callable[[], bool]) -> bool:
        """扫描单个文件；返回 True 表示名额已满、应停止整次搜索。"""
        rel = rel_to_root(path, self.root)
        text = self._read_text(path, rel)
        if text is None:
            return False
        self.files_scanned += 1
        if self.query.multiline:
            return self._scan_multiline(rel, text, should_stop)
        if self.query.with_context:
            return self._scan_with_context(rel, text, should_stop)
        return self._scan_lines(rel, text, should_stop)

    def _scan_lines(self, rel: str, text: str, should_stop: Callable[[], bool]) -> bool:
        """逐行匹配（无上下文）：三种输出模式。"""
        mode = self.query.mode
        matches = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            if lineno % _STOP_CHECK_EVERY_LINES == 0 and should_stop():
                raise SearchStopped
            if self.query.regex.search(line) is None:
                continue
            matches += 1
            if mode == "files_with_matches":
                # 仅文件名：首个命中即可结束本文件
                return self._emit(rel)
            if mode == "content" and self._emit(f"{rel}:{lineno}:{self._clip(line)}"):
                return True
        if mode == "count" and matches:
            return self._emit(f"{rel}:{matches}")
        return False

    def _scan_with_context(self, rel: str, text: str, should_stop: Callable[[], bool]) -> bool:
        """逐行匹配并输出前后文（仅 content 模式）；重叠 / 相邻区间合并为一组。"""
        lines = text.splitlines()
        last_out = 0  # 本文件最后输出的行号（0 = 尚未输出）
        after_left = 0  # 还欠多少行后文
        for lineno, line in enumerate(lines, start=1):
            if lineno % _STOP_CHECK_EVERY_LINES == 0 and should_stop():
                raise SearchStopped
            if self.query.regex.search(line) is not None:
                # 前文从「上次输出之后」与「匹配行 - before」中较晚者开始（重叠区间不重复输出）
                start = max(last_out + 1, lineno - self.query.before)
                if self.used + (lineno - start) + 1 > self.limits.max_results:
                    self.truncated = True  # 整组放不下：不留只有前文的残组
                    return True
                if self.hits and (last_out == 0 or start > last_out + 1):
                    self.hits.append(GROUP_SEPARATOR)  # 与上一组（含上一个文件）不相邻
                for n in range(start, lineno):
                    self._emit(f"{rel}-{n}-{self._clip(lines[n - 1])}", context=True)
                self._emit(f"{rel}:{lineno}:{self._clip(line)}")
                last_out, after_left = lineno, self.query.after
            elif after_left > 0:
                if self._emit(f"{rel}-{lineno}-{self._clip(line)}", context=True):
                    return True
                last_out, after_left = lineno, after_left - 1
        return False

    def _scan_multiline(self, rel: str, text: str, should_stop: Callable[[], bool]) -> bool:
        """整文件匹配（``re.MULTILINE | re.DOTALL``）；content 输出起止行号与片段。"""
        # CRLF 规整为 LF：让 ``$`` 在 Windows 换行的文件里也落在行尾（行号口径不变）
        text = text.replace("\r\n", "\n")
        mode = self.query.mode
        if mode == "files_with_matches":
            return self._emit(rel) if self.query.regex.search(text) is not None else False
        starts = _line_starts(text) if mode == "content" else []
        matches = 0
        for match in self.query.regex.finditer(text):
            matches += 1
            if matches % _STOP_CHECK_EVERY_MATCHES == 0 and should_stop():
                raise SearchStopped
            if mode != "content":
                continue
            first = bisect_right(starts, match.start())
            # 末行取匹配最后一个字符所在行（结尾的换行符属于它结束的那一行）；空匹配取起点
            last_pos = match.end() - 1 if match.end() > match.start() else match.start()
            last = bisect_right(starts, last_pos)
            if self._emit(f"{rel}:{first}-{last}:{self._snippet(match.group(0))}"):
                return True
        if mode == "count" and matches:
            return self._emit(f"{rel}:{matches}")
        return False

    def _read_text(self, path: Path, rel: str) -> str | None:
        """读文件为文本；超大 / 二进制 / 非 UTF-8 / 读失败返回 None 并计入跳过统计。"""
        limit = self.limits.max_file_bytes
        try:
            with path.open("rb") as fh:
                # 多读 1 字节判断是否超限，避免先 stat 再读的竞态
                data = fh.read(limit + 1)
        except OSError:
            self.stats.unreadable += 1
            return None
        if len(data) > limit:
            self.stats.skipped_large.append(rel)
            return None
        if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
            self.stats.skipped_binary += 1
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            self.stats.skipped_binary += 1
            return None

    def _emit(self, line: str, *, context: bool = False) -> bool:
        """占一个名额输出一行；名额已满时标记截断并返回 True（调用方据此停止）。"""
        if self.used >= self.limits.max_results:
            self.truncated = True
            return True
        self.hits.append(line)
        self.used += 1
        if context:
            self.context_lines += 1
        else:
            self.count += 1
        return False

    def _clip(self, line: str) -> str:
        """单行超长时截断并注明原长度（压缩行 / 超长 JSON 行不会撑爆输出）。"""
        cap = self.limits.max_line_chars
        if len(line) <= cap:
            return line
        return f"{line[:cap]} …[line truncated, {len(line)} chars]"

    def _snippet(self, matched: str) -> str:
        """跨行匹配片段：JSON 字符串（换行转义，无歧义）；超长截断并注明原长度。"""
        cap = self.limits.max_line_chars
        body = json.dumps(matched[:cap], ensure_ascii=False)
        if len(matched) <= cap:
            return body
        return f"{body} …[match truncated, {len(matched)} chars]"


__all__ = ["GROUP_SEPARATOR", "GrepLimits", "GrepOutputMode", "GrepQuery", "GrepRun"]
