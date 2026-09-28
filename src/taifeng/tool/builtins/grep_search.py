"""grep —— 沙盒内正则搜索文件内容（opt-in 内置工具，ADR 0064）。

设计要点：
    - 纯 Python（``re``），不依赖外部 rg 二进制；逐行匹配（不支持跨行模式）
    - 三种输出：``content``（``路径:行号:行``，行号 1 基）/ ``files_with_matches`` / ``count``
    - 上限：结果条数 ``max_results``（content 按匹配行计，另两种按文件计）、单行字符
      ``max_line_chars``、单文件字节 ``max_file_bytes``；截断 / 跳过都在输出尾注明确告知
    - 二进制（前 8KB 含 NUL）与非 UTF-8 文件跳过并计数——与 ``file_read`` 同口径
      （file_read 读不了的文件，搜出来也没法继续读）
    - 沙盒 / 符号链接 / 权限 / 取消语义见 ``search_walk``；``parallel_safe=True``、``pure``

已知边界（如实记录）：Python ``re`` 无匹配超时，病态正则在单行上的灾难性回溯无法被打断；
工具超时后 await 立即返回，工作线程在当前行匹配结束后的下一个检查点退出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from taifeng.tool.builtins.file_io import _resolve_safe
from taifeng.tool.builtins.search_walk import (
    DEFAULT_SEARCH_EXCLUDE_DIRS,
    MAX_PATTERN_CHARS,
    GlobMatcher,
    SearchStopped,
    WalkStats,
    cancelled_result,
    check_search_permission,
    compile_glob,
    iter_files,
    rel_to_root,
    run_in_worker,
    skip_notes,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.permission.types import PermissionPolicy

GrepOutputMode = Literal["content", "files_with_matches", "count"]
GREP_OUTPUT_MODES: tuple[GrepOutputMode, ...] = ("content", "files_with_matches", "count")

#: 二进制嗅探窗口：前 8KB 出现 NUL 即判二进制（git / ripgrep 同款启发式）
_BINARY_SNIFF_BYTES = 8192

#: 大文件内每扫多少行检查一次停止信号（逐行检查开销不划算，按批检查足够及时）
_STOP_CHECK_EVERY_LINES = 1024


@dataclass(frozen=True)
class _GrepQuery:
    """一次 grep 调用的已校验参数。"""

    regex: re.Pattern[str]
    include: GlobMatcher | None
    mode: GrepOutputMode
    path: str


@dataclass(frozen=True)
class _GrepLimits:
    """工厂级上限（构造时确定，调用间不变）。"""

    max_results: int
    max_line_chars: int
    max_file_bytes: int


@dataclass
class _GrepRun:
    """工作线程内的一次搜索：遍历、逐文件扫描、按上限收集结果。"""

    root: Path
    query: _GrepQuery
    limits: _GrepLimits
    exclude_dirs: frozenset[str]
    hits: list[str] = field(default_factory=list)
    truncated: bool = False
    files_scanned: int = 0
    stats: WalkStats = field(default_factory=WalkStats)

    def run(self, base: Path, should_stop: Callable[[], bool]) -> None:
        """遍历 ``base``（文件或目录）并扫描；结果条数到上限即停。

        Raises:
            SearchStopped: 取消 / 超时的停止信号。
        """
        files = iter_files(
            base, root=self.root, exclude_dirs=self.exclude_dirs,
            should_stop=should_stop, stats=self.stats,
        )
        for path in files:
            # include 过滤：相对搜索基点比对（基点是文件时只剩文件名一段）
            rel_parts = path.relative_to(base).parts or (path.name,)
            if self.query.include is not None and not self.query.include.matches(rel_parts):
                continue
            if self._scan(path, should_stop):
                return

    def _scan(self, path: Path, should_stop: Callable[[], bool]) -> bool:
        """扫描单个文件；返回 True 表示结果已达上限、应停止整次搜索。"""
        rel = rel_to_root(path, self.root)
        text = self._read_text(path, rel)
        if text is None:
            return False
        self.files_scanned += 1
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
                return self._record(rel)
            if mode == "content" and self._record(f"{rel}:{lineno}:{self._clip(line)}"):
                return True
        if mode == "count" and matches:
            return self._record(f"{rel}:{matches}")
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

    def _record(self, hit: str) -> bool:
        """记录一条结果；已满时标记截断并返回 True（调用方据此停止）。"""
        if len(self.hits) >= self.limits.max_results:
            self.truncated = True
            return True
        self.hits.append(hit)
        return False

    def _clip(self, line: str) -> str:
        """单行超长时截断并注明原长度（压缩行 / 超长 JSON 行不会撑爆输出）。"""
        cap = self.limits.max_line_chars
        if len(line) <= cap:
            return line
        return f"{line[:cap]} …[line truncated, {len(line)} chars]"


def _bad_args(message: str) -> ToolResult:
    """参数错误的统一形状。"""
    return ToolResult.error(f"bad_args: {message}", reason="bad_args")


def _parse_regex(args: dict[str, Any]) -> re.Pattern[str] | ToolResult:
    """校验 ``pattern`` / ``ignore_case`` 并编译正则；失败返回 bad_args 结果。"""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _bad_args("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        return _bad_args(f"pattern longer than {MAX_PATTERN_CHARS} chars")
    # 可选参数缺省即关闭忽略大小写（schema 声明的缺省值，不是兜底）
    ignore_case = args.get("ignore_case", False)
    if not isinstance(ignore_case, bool):
        return _bad_args("ignore_case must be boolean")
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return _bad_args(f"invalid regex: {exc}")


def _parse_include(raw: Any) -> GlobMatcher | ToolResult | None:
    """校验并编译 ``include``；未提供返回 None，非法返回 bad_args 结果。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return _bad_args("include must be string")
    try:
        return compile_glob(raw, basename_if_no_slash=True)
    except ValueError as exc:
        return _bad_args(f"include: {exc}")


def _parse_args(args: dict[str, Any]) -> _GrepQuery | ToolResult:
    """校验 LLM 参数并编译正则 / include；失败返回 bad_args 结果。"""
    regex = _parse_regex(args)
    if isinstance(regex, ToolResult):
        return regex
    # output_mode / path 缺省值即 schema 声明的缺省语义（content / 沙盒根）
    mode = args.get("output_mode", "content")
    if mode not in GREP_OUTPUT_MODES:
        return _bad_args(f"output_mode must be one of {list(GREP_OUTPUT_MODES)}")
    path = args.get("path", ".")
    if not isinstance(path, str):
        return _bad_args("path must be string")
    include = _parse_include(args.get("include"))
    if isinstance(include, ToolResult):
        return include
    return _GrepQuery(regex=regex, include=include, mode=mode, path=path)


def _render(run: _GrepRun) -> ToolResult:
    """把收集结果渲染成 LLM 可见文本 + telemetry data。"""
    unit = "matching lines" if run.query.mode == "content" else "files"
    lines = list(run.hits) if run.hits else ["no matches found"]
    if run.truncated:
        lines.append(
            f"[results truncated at {run.limits.max_results} {unit}; "
            "narrow pattern / path / include to see the rest]"
        )
    lines.extend(skip_notes(run.stats, max_file_bytes=run.limits.max_file_bytes))
    return ToolResult.ok(
        "\n".join(lines),
        mode=run.query.mode,
        count=len(run.hits),
        truncated=run.truncated,
        files_scanned=run.files_scanned,
        skipped_binary=run.stats.skipped_binary,
        skipped_large=len(run.stats.skipped_large),
        skipped_symlinks=run.stats.skipped_symlinks,
        unreadable=run.stats.unreadable,
    )


#: grep 的 input_schema（additionalProperties=false，内核派发前预校验）
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Python 正则（re 语法），逐行匹配"},
        "path": {
            "type": "string",
            "description": "搜索基点：相对沙盒根的目录或文件（缺省=沙盒根）",
        },
        "include": {
            "type": "string",
            "description": "文件过滤 glob，如 *.py / **/*.{ts,tsx}；不含 / 时只比对文件名",
        },
        "ignore_case": {"type": "boolean", "description": "忽略大小写（缺省 false）"},
        "output_mode": {
            "type": "string",
            "enum": list(GREP_OUTPUT_MODES),
            "description": (
                "content=路径:行号:行（缺省）；files_with_matches=仅文件路径；"
                "count=路径:匹配行数"
            ),
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def make_grep_tool(
    *,
    root_dir: str | Path,
    policy: PermissionPolicy | None = None,
    max_results: int = 200,
    max_line_chars: int = 500,
    max_file_bytes: int = 2 * 1024 * 1024,
    exclude_dirs: frozenset[str] = DEFAULT_SEARCH_EXCLUDE_DIRS,
    timeout_seconds: float = 30.0,
) -> ToolSpec:
    """构造 grep 工具（opt-in：经 ``EnginePool.create(extra_tools=[...])`` 注册）。

    Args:
        root_dir: 沙盒根；搜索基点与所有被读文件都必须落在其内（同 file_read）。
        policy: 可选权限策略；每次调用以 ``scope="file_read"``、target=搜索基点绝对路径审批一次。
        max_results: 结果条数上限（content 按匹配行、其余按文件）；超出截断并告知。
        max_line_chars: content 模式单行字符上限。
        max_file_bytes: 单文件字节上限（默认 2MB）；更大的文件跳过并在尾注列出。
        exclude_dirs: 不下探的目录名集合（默认 ``DEFAULT_SEARCH_EXCLUDE_DIRS``）。
        timeout_seconds: 单次调用超时（ToolSpec 级，超时由 runtime 统一处理）。

    Raises:
        ValueError: 上限参数非正。
    """
    if min(max_results, max_line_chars, max_file_bytes) <= 0:
        raise ValueError("max_results / max_line_chars / max_file_bytes must be > 0")
    root = Path(root_dir).expanduser().resolve()
    limits = _GrepLimits(
        max_results=max_results, max_line_chars=max_line_chars, max_file_bytes=max_file_bytes,
    )

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = _parse_args(args)
        if isinstance(parsed, ToolResult):
            return parsed
        if ctx.cancel.is_cancelled:
            return cancelled_result(ctx)
        base = _resolve_safe(root, parsed.path)
        if base is None:
            return ToolResult.error(
                f"path_outside_sandbox: {parsed.path} (root={root})", reason="sandbox_violation",
            )
        denied = await check_search_permission(
            policy, target=base, ctx=ctx, tool_name="grep", pattern=parsed.regex.pattern,
        )
        if denied is not None:
            return denied
        if not base.exists():
            return ToolResult.error(f"not_found: {parsed.path}", reason="not_found")
        run = _GrepRun(root=root, query=parsed, limits=limits, exclude_dirs=exclude_dirs)
        try:
            await run_in_worker(lambda stop: run.run(base, stop), ctx.cancel)
        except SearchStopped:
            if ctx.cancel.is_cancelled:
                return cancelled_result(ctx)
            raise  # 停止信号只来自 token 取消或 await 被放弃，走到这里是内部契约被破坏
        return _render(run)

    return ToolSpec(
        name="grep",
        description=(
            f"在沙盒（root={root}）内按正则搜索文件内容，纯只读。"
            f"结果最多 {max_results} 条（超出会截断并提示收窄条件）；单行超过 "
            f"{max_line_chars} 字符截断；大于 {max_file_bytes} 字节的文件、二进制与非 UTF-8 "
            "文件跳过并在尾注告知。结果按路径排序，路径相对沙盒根、可直接用于 file_read；"
            "content 模式行号 1 基（file_read 的 offset = 行号 - 1）。"
        ),
        input_schema=_SCHEMA,
        handler=handler,
        parallel_safe=True,
        # 只读：崩溃恢复可安全重发，strict audit 记无副作用
        effect_kind="pure",
        reconciliation="none",
        timeout_seconds=timeout_seconds,
    )


__all__ = ["GREP_OUTPUT_MODES", "GrepOutputMode", "make_grep_tool"]
