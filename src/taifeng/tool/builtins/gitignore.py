""".gitignore 规则的纯 Python 解析与匹配（glob / grep 遍历用，ADR 0071）。

只实现 glob / grep 需要的那部分 gitignore 语义，不调用 git、不引第三方依赖：

- 空行忽略；``#`` 开头是注释（``\\#`` 转义为字面量）；行尾未转义的空格去掉（``\\ `` 保留）；
- ``!`` 前缀否定（``\\!`` 转义为字面量）：同一文件内**最后一条**命中的规则说了算，
  子目录的 ``.gitignore`` 优先于父目录（离路径越近越优先）——与 git 的层级优先序一致；
- 尾斜杠 ``foo/`` 只匹配目录；
- 含 ``/``（开头或中间）的模式锚定在该 ``.gitignore`` 所在目录；不含 ``/`` 的模式在其下任意深度
  按名称匹配；开头的 ``/`` 只表示锚定；
- ``*`` / ``?`` / ``[...]``（``[!...]`` / ``[^...]`` 取反）不跨 ``/``；``**`` 作为独立一段时：
  开头 ``**/x`` = 任意深度的 x，结尾 ``x/**`` = x 内的一切，中间 ``a/**/b`` = 零或多层目录；
- 目录被忽略后整棵子树不再下探，因此「父目录被忽略的文件无法经 ``!`` 重新纳入」与 git 一致。

**不支持**（如实记录）：POSIX 字符类（``[[:alpha:]]`` 等）——含它的行跳过并计数，在工具输出尾注告知，
规则对应的路径**不会**被跳过；``.git/info/exclude``、全局 ``core.excludesFile``、``.ignore`` /
``.rgignore``；沙盒根之上的 ``.gitignore``；``core.ignorecase``（一律大小写敏感）；
「已被 git 跟踪的文件不受忽略规则影响」（本实现不读 git 索引）；符号链接形式的 ``.gitignore``
不读取（同 git 2.32+）。

参照：git ``Documentation/gitignore.txt`` 与 ``dir.c`` 的匹配顺序；ripgrep ``ignore`` crate 的
「规则按路径逐条判定、被忽略目录不下探」遍历方式。差异：纯 Python、只读工作区内的 ``.gitignore``。
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: 单个 .gitignore 读取上限（字节）；更大的文件视为不可读（计入 unreadable 并告知）
MAX_GITIGNORE_BYTES = 1024 * 1024

#: 需要在 Python 字符类里转义的字符（``-`` 保留以支持区间）
_CLASS_SPECIALS = frozenset("\\]^[")


class UnsupportedIgnorePattern(ValueError):  # noqa: N818 —— 语义是「不支持的语法」
    """该行 gitignore 语法本实现不支持（如 POSIX 字符类、未闭合的 ``[``、行尾孤立反斜杠）。"""


@dataclass(frozen=True)
class IgnoreRule:
    """一条已编译的 gitignore 规则。

    Attributes:
        regex: 对「相对该 .gitignore 所在目录的 POSIX 路径」做 ``fullmatch`` 的正则。
        negated: ``!`` 前缀——命中时表示「不忽略」。
        dir_only: 尾斜杠——只匹配目录。
    """

    regex: re.Pattern[str]
    negated: bool
    dir_only: bool

    def matches(self, rel: str, *, is_dir: bool) -> bool:
        """``rel`` 是相对规则所在目录的 POSIX 路径；``is_dir`` 为该路径是否目录。"""
        if self.dir_only and not is_dir:
            return False
        return self.regex.fullmatch(rel) is not None


@dataclass(frozen=True)
class IgnoreLevel:
    """一个目录下 .gitignore 的全部规则。

    Attributes:
        base_parts: 该 .gitignore 所在目录相对沙盒根的路径分段（根目录为空元组）。
        rules: 按文件内顺序排列的规则。
    """

    base_parts: tuple[str, ...]
    rules: tuple[IgnoreRule, ...]


def _strip_trailing_spaces(line: str) -> str:
    """去掉行尾未转义的空格（``\\ `` 结尾的保留，与 git 一致）。"""
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    return line


def _translate_class(seg: str, start: int) -> tuple[str, int]:
    """把 ``seg[start]`` 处的 ``[...]`` 译成 Python 字符类，返回 ``(正则片段, 结束后下标)``。

    ``[!...]`` / ``[^...]`` 取反；紧跟在 ``[`` / ``[!`` 之后的 ``]`` 是字面量；
    ``\\x`` 转义为字面 x。
    取反类额外排除 ``/``（通配不跨目录）。

    Raises:
        UnsupportedIgnorePattern: 未闭合，或含 POSIX 字符类 ``[:name:]``。
    """
    i = start + 1
    negate = i < len(seg) and seg[i] in "!^"
    if negate:
        i += 1
    body: list[str] = []
    first = True
    while i < len(seg):
        c = seg[i]
        if c == "]" and not first:
            prefix = "[^/" if negate else "["
            return prefix + "".join(body) + "]", i + 1
        if c == "[" and seg.startswith("[:", i):
            raise UnsupportedIgnorePattern("POSIX character classes are not supported")
        if c == "\\" and i + 1 < len(seg):
            # 转义：下一个字符按字面量进字符类
            i += 1
            c = seg[i]
        body.append("\\" + c if c in _CLASS_SPECIALS else c)
        first = False
        i += 1
    raise UnsupportedIgnorePattern("unclosed '[' in pattern")


def _translate_segment(seg: str) -> str:
    """把不含 ``/`` 的一段通配译成正则片段（``*`` / ``?`` / ``[...]`` 不跨目录）。

    Raises:
        UnsupportedIgnorePattern: 行尾孤立反斜杠或字符类不受支持。
    """
    out: list[str] = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "\\":
            if i + 1 >= len(seg):
                raise UnsupportedIgnorePattern("trailing backslash")
            out.append(re.escape(seg[i + 1]))
            i += 2
            continue
        if c == "[":
            piece, i = _translate_class(seg, i)
            out.append(piece)
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _translate(pattern: str) -> str:
    """把去掉锚定 ``/`` 与尾斜杠后的模式按 ``/`` 分段译成正则（``**`` 段特殊处理）。"""
    segs = pattern.split("/")
    out: list[str] = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            # 结尾 ** 吞掉目录内一切；开头 / 中间 ** 吞零或多层目录（含其后的 /）
            out.append(".*" if last else "(?:.*/)?")
            continue
        out.append(_translate_segment(seg))
        if not last:
            out.append("/")
    return "".join(out)


def compile_rule(line: str) -> IgnoreRule | None:
    """编译一行 gitignore；空行 / 注释 / 空模式返回 None。

    Raises:
        UnsupportedIgnorePattern: 该行语法不受支持（调用方跳过并计数，不静默）。
    """
    line = _strip_trailing_spaces(line)
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    # 去掉尾斜杠后仍含 / 即锚定（开头 / 仅表示锚定，本身不参与匹配）
    anchored = "/" in line
    line = line.removeprefix("/")
    if not line:
        return None
    body = _translate(line)
    source = body if anchored else f"(?:.*/)?{body}"
    return IgnoreRule(regex=re.compile(source, re.DOTALL), negated=negated, dir_only=dir_only)


def parse_gitignore(text: str) -> tuple[tuple[IgnoreRule, ...], int]:
    """解析整个 .gitignore 文本，返回 ``(规则, 不支持而跳过的行数)``。"""
    rules: list[IgnoreRule] = []
    unsupported = 0
    for raw in text.removeprefix("﻿").splitlines():
        try:
            rule = compile_rule(raw)
        except UnsupportedIgnorePattern:
            unsupported += 1
            continue
        if rule is not None:
            rules.append(rule)
    return tuple(rules), unsupported


def read_gitignore(directory: Path) -> str | None:
    """读取 ``directory/.gitignore`` 的文本；不存在或不是普通文件（含符号链接）返回 None。

    Raises:
        OSError: 读取失败、超过 ``MAX_GITIGNORE_BYTES`` 或不是合法 UTF-8（调用方计入 unreadable）。
    """
    path = directory / ".gitignore"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    # 只读普通文件：符号链接不跟随（防读沙盒外内容，且与 git 2.32+ 行为一致）
    if not stat.S_ISREG(info.st_mode):
        return None
    with path.open("rb") as fh:
        data = fh.read(MAX_GITIGNORE_BYTES + 1)
    if len(data) > MAX_GITIGNORE_BYTES:
        raise OSError(f"{path} larger than {MAX_GITIGNORE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError(f"{path} is not valid UTF-8") from exc


def is_ignored(levels: Sequence[IgnoreLevel], rel_parts: tuple[str, ...], *, is_dir: bool) -> bool:
    """按 git 优先序判定路径是否被忽略。

    ``levels`` 由浅到深排列且每层的 ``base_parts`` 都是 ``rel_parts`` 的前缀（遍历栈保证）。
    从最深一层、每层从最后一条规则往前找，第一条命中的规则决定结果（``!`` 命中 = 不忽略）；
    全部不命中 = 不忽略。
    """
    for level in reversed(levels):
        sub = "/".join(rel_parts[len(level.base_parts):])
        for rule in reversed(level.rules):
            if rule.matches(sub, is_dir=is_dir):
                return not rule.negated
    return False


__all__ = [
    "MAX_GITIGNORE_BYTES",
    "IgnoreLevel",
    "IgnoreRule",
    "UnsupportedIgnorePattern",
    "compile_rule",
    "is_ignored",
    "parse_gitignore",
    "read_gitignore",
]
