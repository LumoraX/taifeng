"""glob / grep 共用的沙盒遍历、glob 匹配与后台线程执行原语。

设计要点：
    - **沙盒一致性**：搜索基点经 ``file_io._resolve_safe`` 解析（跟随符号链接后仍须
      落在 root 内），与 ``file_read`` 同一判定；遍历中**不跟随**指向目录的符号链接
      （防环、防逃逸），指向文件的符号链接仅在目标解析后仍在 root 内时才纳入。
    - **确定性顺序**：深度优先、同层按名称（码点序）排序、文件与目录交错——结果顺序
      等价于 ``sorted(paths, key=lambda p: p.parts)``，即按路径逐段字典序。选它而不选
      修改时间的理由见 ADR 0064：同样的调用得到同样的输出（pure 语义 / 回放 / cache
      友好），且能在命中 ``max_results + 1`` 时提前停止遍历，不必先 stat 全量再排序。
    - **R4 可取消**：阻塞 IO 在 ``anyio.to_thread`` 工作线程里跑，线程在每个目录项 /
      每批行处检查停止信号；token 取消与 await 被放弃（超时 / 外部取消）都会点亮它。

    - **.gitignore**（ADR 0071）：``gitignore=True`` 时按 ``gitignore`` 模块的语义逐条判定
      遍历到的路径，被忽略的目录不下探、被忽略的路径计数并告知；规则来自沙盒根到搜索基点
      沿途各级目录与遍历中进入的子目录。搜索基点自身不受忽略规则影响（显式指定即照常搜索）。

    - **文件访问经 ``search_fs``**（ADR 0113）：遍历只认 ``SearchFs`` 的同步接口，本机目录与
      注入的 ``WorkspaceFS`` 走同一套逻辑；路径一律是 ``PurePath``（本机 ``Path``，非本机是
      虚拟根 ``/`` 下的 ``PurePosixPath``）。

参照：ripgrep ``ignore::WalkBuilder``（默认不跟随符号链接、噪声目录排除、按 .gitignore 剪枝）与
Claude Code Glob / Grep 工具的 LLM 侧形状；差异：纯 Python 实现、只读工作区内的 .gitignore、
结果按路径排序而非 mtime。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.to_thread

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.builtins.gitignore import (
    IgnoreLevel,
    is_ignored,
    parse_gitignore,
)
from taifeng.tool.builtins.search_fs import LocalSearchFs, SearchEntry, SearchFs
from taifeng.tool.spec import ToolContext, ToolResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import PurePath

    from taifeng.loop.cancellation import CancellationToken

#: 默认排除的噪声目录（按目录名精确匹配，任意深度）。业务可经工厂 ``exclude_dirs`` 覆盖，
#: 例如 ``DEFAULT_SEARCH_EXCLUDE_DIRS | {"dist"}``。显式把某个排除目录作为搜索基点时不受影响。
DEFAULT_SEARCH_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",                          # 版本库元数据
    "node_modules", ".venv", "__pycache__",         # 依赖 / 虚拟环境 / 字节码
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",  # 工具缓存
})

#: 模式串长度上限：glob / 正则 / include 都受此约束，防超长输入拖垮匹配
MAX_PATTERN_CHARS = 1000

#: 花括号展开的组合上限：``{a,b}{c,d}...`` 会指数膨胀，超限按 bad_args 拒绝
_MAX_BRACE_EXPANSIONS = 64

#: 输出里列出的「超大而跳过」文件名个数上限（其余只计数）
_MAX_LISTED_SKIPS = 5


class SearchStopped(Exception):  # noqa: N818 —— 语义是「被叫停」而非错误
    """工作线程收到停止信号（token 取消 / await 被放弃）后提前退出。"""


@dataclass
class WalkStats:
    """遍历期的跳过计数（工作线程写、事件循环线程在线程结束后读）。"""

    skipped_symlinks: int = 0
    """未跟随的符号链接：指向目录、指向沙盒外或悬空。"""

    unreadable: int = 0
    """无法读取的目录 / 文件（权限不足等 OSError）。"""

    skipped_large: list[str] = field(default_factory=list)
    """超过单文件大小上限而跳过的文件（相对 root 的路径，仅 grep 使用）。"""

    skipped_binary: int = 0
    """二进制（含 NUL 字节）或非 UTF-8 而跳过的文件数（仅 grep 使用）。"""

    skipped_ignored: int = 0
    """被 .gitignore 规则忽略的路径数（被忽略的目录按 1 计，其子树不再遍历）。"""

    gitignore_unsupported: int = 0
    """.gitignore 中语法不受支持而未生效的行数（这些规则对应的路径**没有**被跳过）。"""


# ---------------------------------------------------------------------------
# glob 模式编译与匹配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlobMatcher:
    """编译后的 glob：花括号已展开、按 ``/`` 切段。

    ``basename_only=True`` 时只拿文件名比对（grep ``include`` 不含 ``/`` 时的语义，
    与 ``grep --include`` / ``rg --glob`` 一致）。
    """

    alternatives: tuple[tuple[str, ...], ...]
    basename_only: bool = False

    def matches(self, rel_parts: tuple[str, ...]) -> bool:
        """``rel_parts`` 是相对搜索基点的路径分段；任一展开式命中即算命中。"""
        target = rel_parts[-1:] if self.basename_only else rel_parts
        return any(_match_parts(alt, target) for alt in self.alternatives)


def compile_glob(pattern: str, *, basename_if_no_slash: bool = False) -> GlobMatcher:
    """校验并编译 glob 模式。

    支持 ``*``（段内任意）/ ``?`` / ``[...]`` / ``**``（零或多段目录）/ ``{a,b}``（可嵌套）。
    大小写敏感（``fnmatchcase``）。

    Args:
        pattern: 相对搜索基点的模式。
        basename_if_no_slash: True 且模式不含 ``/`` 时只比对文件名（grep include 语义）。

    Raises:
        ValueError: 空模式 / 超长 / 绝对路径 / 含 ``..`` 段 / 花括号不配对或展开过多。
    """
    if not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern longer than {MAX_PATTERN_CHARS} chars")
    if pattern.startswith("/"):
        raise ValueError("pattern must be relative (use `path` to choose the search base)")
    alternatives: list[tuple[str, ...]] = []
    for expanded in expand_braces(pattern):
        # 规整：去掉空段（``a//b`` / 尾随 ``/``）与 ``.`` 段（``./src``）
        parts = tuple(seg for seg in expanded.split("/") if seg not in ("", "."))
        if ".." in parts:
            raise ValueError("pattern must not contain '..' segments")
        if not parts:
            raise ValueError("pattern has no path segments")
        alternatives.append(parts)
    return GlobMatcher(
        alternatives=tuple(alternatives),
        basename_only=basename_if_no_slash and "/" not in pattern,
    )


def expand_braces(pattern: str) -> list[str]:
    """展开花括号 ``{a,b}``（可嵌套）；无花括号时原样返回单元素列表。

    ``{x}``（无逗号）按单一选项 ``x`` 处理；孤立的 ``}`` 视为字面量。

    Raises:
        ValueError: ``{`` 不配对，或展开结果超过上限。
    """
    done: list[str] = []
    pending = [pattern]
    while pending:
        current = pending.pop()
        group = _first_brace_group(current)
        if group is None:
            done.append(current)
        else:
            start, end, options = group
            pending.extend(current[:start] + opt + current[end + 1:] for opt in options)
        if len(done) + len(pending) > _MAX_BRACE_EXPANSIONS:
            raise ValueError(f"brace expansion exceeds {_MAX_BRACE_EXPANSIONS} patterns")
    return done


def _first_brace_group(text: str) -> tuple[int, int, list[str]] | None:
    """找第一个 ``{...}`` 组，返回 ``(起, 止, 顶层逗号切出的选项)``；无 ``{`` 返回 None。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    options: list[str] = []
    seg_start = start + 1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                options.append(text[seg_start:i])
                return start, i, options
        elif ch == "," and depth == 1:
            # 只在最外层组内切分；内层组留给下一轮展开
            options.append(text[seg_start:i])
            seg_start = i + 1
    raise ValueError("unbalanced '{' in pattern")


def _match_parts(pat: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """逐段匹配：普通段走 ``fnmatchcase``，``**`` 段吞掉零或多段。

    记忆化失败的 ``(模式下标, 路径下标)`` 组合，最坏复杂度 O(len(pat) × len(parts))，
    多个 ``**`` 也不会指数回溯。
    """
    failed: set[tuple[int, int]] = set()

    def match_from(i: int, j: int) -> bool:
        """模式从下标 i、路径从下标 j 起能否完整匹配到末尾。"""
        if (i, j) in failed:
            return False
        start = (i, j)
        # 先线性吃掉连续的普通段
        while i < len(pat) and pat[i] != "**":
            if j >= len(parts) or not fnmatchcase(parts[j], pat[i]):
                failed.add(start)
                return False
            i += 1
            j += 1
        if i == len(pat):
            ok = j == len(parts)
        else:
            # pat[i] == "**"：尝试让它吞掉 parts[j:k]（k 从 j 到末尾）
            ok = any(match_from(i + 1, k) for k in range(j, len(parts) + 1))
        if not ok:
            failed.add(start)
        return ok

    return match_from(0, 0)


# ---------------------------------------------------------------------------
# 沙盒遍历
# ---------------------------------------------------------------------------


@dataclass
class _Walk:
    """一次遍历的可变状态：目录项迭代栈，以及与之对齐的 .gitignore 规则层栈。

    栈帧 = ``(目录项迭代器, 该目录是否压了规则层)``；弹出目录时同步弹出它的规则层，
    保证 ``levels`` 始终只含「当前路径的祖先目录」的规则（``is_ignored`` 的前提）。
    """

    root: PurePath
    exclude_dirs: frozenset[str]
    stats: WalkStats
    gitignore: bool
    fs: SearchFs
    stack: list[tuple[Iterator[SearchEntry], bool]] = field(default_factory=list)
    levels: list[IgnoreLevel] = field(default_factory=list)

    def _path(self, entry: SearchEntry) -> PurePath:
        """目录项的路径，与 ``root`` 同一种 ``PurePath``。"""
        return type(self.root)(entry.path)

    def load_ancestors(self, base: PurePath) -> None:
        """载入沙盒根到 ``base`` 父目录沿途各级的 .gitignore（常驻，不随栈弹出）。"""
        parts = base.relative_to(self.root).parts
        for depth in range(len(parts)):
            level = self._load_level(self.root.joinpath(*parts[:depth]))
            if level is not None:
                self.levels.append(level)

    def enter(self, directory: PurePath) -> None:
        """目录压栈；启用 gitignore 且该目录有生效规则时同步压入规则层。"""
        level = self._load_level(directory) if self.gitignore else None
        if level is not None:
            self.levels.append(level)
        self.stack.append((_sorted_entries(self.fs, directory, self.stats), level is not None))

    def leave(self) -> None:
        """当前目录遍历完毕：弹栈，并弹出它压入的规则层。"""
        _, owns_level = self.stack.pop()
        if owns_level:
            self.levels.pop()

    def admit(self, entry: SearchEntry) -> PurePath | None:
        """判定一个目录项：文件返回其路径；目录压栈后返回 None；其余跳过（计数）。"""
        is_dir = entry.is_dir(follow_symlinks=False)
        if is_dir and entry.name in self.exclude_dirs:
            return None
        path = self._path(entry)
        if self.levels and is_ignored(
            self.levels, path.relative_to(self.root).parts, is_dir=is_dir,
        ):
            # 被忽略的目录整棵子树不下探（与 git「父目录被忽略则子路径无法重新纳入」一致）
            self.stats.skipped_ignored += 1
            return None
        if entry.is_symlink():
            if self.fs.symlink_file_inside(entry):
                return path
            self.stats.skipped_symlinks += 1
            return None
        if is_dir:
            self.enter(path)
            return None
        return path if entry.is_file(follow_symlinks=False) else None

    def _load_level(self, directory: PurePath) -> IgnoreLevel | None:
        """读取并解析 ``directory/.gitignore``；无文件 / 无有效规则返回 None。

        读不了（权限 / 超大 / 非 UTF-8）计入 ``unreadable``；不支持的行计入
        ``gitignore_unsupported``——两者都会在输出尾注告知，不静默。
        """
        try:
            text = self.fs.read_gitignore(directory)
        except OSError:
            self.stats.unreadable += 1
            return None
        if text is None:
            return None
        rules, unsupported = parse_gitignore(text)
        self.stats.gitignore_unsupported += unsupported
        if not rules:
            return None
        return IgnoreLevel(base_parts=directory.relative_to(self.root).parts, rules=rules)


def iter_files[P: PurePath](
    base: P,
    *,
    root: P,
    exclude_dirs: frozenset[str],
    should_stop: Callable[[], bool],
    stats: WalkStats,
    gitignore: bool = False,
    fs: SearchFs | None = None,
) -> Iterator[P]:
    """按路径逐段字典序深度优先产出 ``base`` 下的文件（``base`` 是文件时只产出它自己）。

    - 目录名在 ``exclude_dirs`` 内 → 不下探（``base`` 自身不受此限）；
    - ``gitignore=True`` → 沿途 .gitignore 命中的路径跳过并计数，被忽略目录不下探
      （``base`` 自身不受此限：显式指定的基点照常搜索，其下路径仍逐条判定）；
    - 符号链接：指向 root 内文件 → 以链接路径产出；指向目录 / root 外 / 悬空 → 跳过并计数；
    - 读不了的目录 → 计数后继续（不静默吞，渲染时明确告知）。

    ``fs`` 不给时按本机目录访问（``root`` 须是 ``Path``）。

    Raises:
        SearchStopped: ``should_stop()`` 为真（每个目录项检查一次）。
    """
    if fs is None:
        fs = LocalSearchFs(Path(root))
    if fs.is_file(base):
        yield base
        return
    walk = _Walk(root=root, exclude_dirs=exclude_dirs, stats=stats, gitignore=gitignore, fs=fs)
    if gitignore:
        walk.load_ancestors(base)
    walk.enter(base)
    while walk.stack:
        entry = next(walk.stack[-1][0], None)
        if entry is None:
            walk.leave()
            continue
        if should_stop():
            raise SearchStopped
        path = walk.admit(entry)
        if path is not None:
            yield path  # type: ignore[misc]  # 与 root 同一种 PurePath（见 _Walk._path）


def _sorted_entries(
    fs: SearchFs, directory: PurePath, stats: WalkStats,
) -> Iterator[SearchEntry]:
    """读出目录项并按名称排序；读失败计入 ``stats.unreadable`` 并返回空迭代器。"""
    try:
        entries = sorted(fs.scandir(directory), key=lambda e: e.name)
    except OSError:
        stats.unreadable += 1
        return iter(())
    return iter(entries)


def rel_to_root(path: PurePath, root: PurePath) -> str:
    """相对 root 的 POSIX 风格路径——可直接作为 file_read / apply_patch 的 ``path`` 参数。"""
    return path.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# 后台线程执行（R4）
# ---------------------------------------------------------------------------


async def run_in_worker[T](
    fn: Callable[[Callable[[], bool]], T], cancel: CancellationToken,
) -> T:
    """把阻塞函数 ``fn(should_stop)`` 放到工作线程执行，并把取消接到停止信号上。

    - token 取消 → ``on_cancel`` 回调点亮停止信号，线程在下一个检查点抛 ``SearchStopped``；
    - await 被取消（工具超时 ``asyncio.wait_for`` / 外部 task 取消）→ ``abandon_on_cancel``
      让 await 立即返回，``finally`` 点亮停止信号，被放弃的线程随即自行退出，不空转到底。

    Raises:
        SearchStopped: 由 ``fn`` 抛出（调用方据 ``cancel.is_cancelled`` 转成 cancelled 结果）。
    """
    stop = threading.Event()
    remove = cancel.on_cancel(stop.set)
    try:
        return await anyio.to_thread.run_sync(fn, stop.is_set, abandon_on_cancel=True)
    finally:
        stop.set()
        remove()


def cancelled_result(ctx: ToolContext) -> ToolResult:
    """取消的统一结果形状（与 shell_exec 一致：``cancelled (<reason>)``）。"""
    return ToolResult.error(f"cancelled ({ctx.cancel.reason})", reason="cancelled")


# ---------------------------------------------------------------------------
# 权限与渲染
# ---------------------------------------------------------------------------


async def check_search_permission(
    policy: PermissionPolicy | None,
    *,
    target: PurePath | str,
    ctx: ToolContext,
    tool_name: str,
    pattern: str,
) -> ToolResult | None:
    """按 ``file_read`` 效果过权限策略；被拒返回错误结果，放行 / 无策略返回 None。

    粒度是**一次调用一次审批**：target = 搜索基点在工作区里的规范路径（目录或文件），批准即意味着
    允许读取该子树（排除目录除外）。不逐文件审批——一次 grep 可能触及成千上万个文件，
    ``ask`` 模式逐个弹窗不可用。需要更细的隔离请缩小 ``root_dir`` 或配置 ``exclude_dirs``。
    ``metadata.call_id`` 供 resume 时 ``preapprove`` 配对（与 file_read 一致）。
    """
    if policy is None:
        return None
    req = PermissionRequest(
        scope="file_read",
        target=str(target),
        reason=f"LLM 请求搜索文件（{tool_name}）",
        metadata={
            "thread_id": ctx.thread_id,
            "call_id": ctx.call_id,
            "submission_id": ctx.extras.get("submission_id"),
            "tool": tool_name,
            "pattern": pattern,
        },
    )
    decision = await policy.check(req)
    if decision.granted:
        return None
    return ToolResult.error(
        f"permission_denied: {decision.reason}", reason="permission_denied",
    )


def skip_notes(stats: WalkStats, *, max_file_bytes: int | None = None) -> list[str]:
    """把跳过计数渲染成给模型看的尾注行（全为零时返回空列表）。"""
    notes: list[str] = []
    if stats.skipped_large:
        listed = ", ".join(stats.skipped_large[:_MAX_LISTED_SKIPS])
        more = len(stats.skipped_large) - _MAX_LISTED_SKIPS
        tail = f" (+{more} more)" if more > 0 else ""
        notes.append(
            f"[skipped {len(stats.skipped_large)} file(s) larger than "
            f"{max_file_bytes} bytes: {listed}{tail}]"
        )
    if stats.skipped_binary:
        notes.append(f"[skipped {stats.skipped_binary} binary or non-UTF-8 file(s)]")
    if stats.skipped_symlinks:
        notes.append(
            f"[skipped {stats.skipped_symlinks} symlink(s): directory links are not "
            "followed; links outside the sandbox or dangling are ignored]"
        )
    if stats.unreadable:
        notes.append(f"[skipped {stats.unreadable} unreadable path(s)]")
    if stats.skipped_ignored:
        notes.append(
            f"[skipped {stats.skipped_ignored} path(s) ignored by .gitignore; pass an ignored "
            "directory as `path` to search inside it]"
        )
    if stats.gitignore_unsupported:
        notes.append(
            f"[{stats.gitignore_unsupported} .gitignore line(s) use unsupported syntax and were "
            "not applied; paths they target were searched]"
        )
    return notes


__all__ = [
    "DEFAULT_SEARCH_EXCLUDE_DIRS",
    "MAX_PATTERN_CHARS",
    "GlobMatcher",
    "SearchStopped",
    "WalkStats",
    "cancelled_result",
    "check_search_permission",
    "compile_glob",
    "expand_braces",
    "iter_files",
    "rel_to_root",
    "run_in_worker",
    "skip_notes",
]
