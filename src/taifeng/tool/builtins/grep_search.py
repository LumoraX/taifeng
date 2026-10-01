"""grep —— 沙盒内正则搜索文件内容（opt-in 内置工具，ADR 0064 / 0071）。

设计要点：
    - 纯 Python（``re``），不依赖外部 rg 二进制；缺省逐行匹配，``multiline=true`` 时整文件
      匹配（``re.MULTILINE | re.DOTALL``：``^`` / ``$`` 匹配每行首尾、``.`` 可匹配换行）
    - 三种输出：``content``（``路径:行号:行``，行号 1 基；可带 ``context_before`` /
      ``context_after`` / ``context`` 上下文行）/ ``files_with_matches`` / ``count``
    - 上限：结果名额 ``max_results``（content 按输出行计——匹配行与上下文行都占名额；另两种
      按文件计）、单行 / 单片段字符 ``max_line_chars``、单文件字节 ``max_file_bytes``；截断 /
      跳过都在输出尾注明确告知
    - 二进制（前 8KB 含 NUL）与非 UTF-8 文件跳过并计数——与 ``file_read`` 同口径
      （file_read 读不了的文件，搜出来也没法继续读）
    - ``respect_gitignore``（工厂参数，默认 True）：按 .gitignore 跳过路径并告知
      （语义见 ``gitignore`` 模块）
    - 沙盒 / 符号链接 / 权限 / 取消语义见 ``search_walk``；扫描实现见 ``grep_scan``；
      ``parallel_safe=True``、``pure``

已知边界（如实记录）：Python ``re`` 无匹配超时，病态正则的灾难性回溯无法被打断（逐行模式在
单行上，multiline 模式在整个文件上）；工具超时后 await 立即返回，工作线程在当前匹配结束后的
下一个检查点退出。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from taifeng.tool.builtins.file_io import _is_nonneg_int
from taifeng.tool.builtins.grep_scan import GrepLimits, GrepOutputMode, GrepQuery, GrepRun
from taifeng.tool.builtins.search_fs import search_scope
from taifeng.tool.builtins.search_walk import (
    DEFAULT_SEARCH_EXCLUDE_DIRS,
    MAX_PATTERN_CHARS,
    GlobMatcher,
    SearchStopped,
    cancelled_result,
    check_search_permission,
    compile_glob,
    run_in_worker,
    skip_notes,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from taifeng.tool.workspace import WorkspaceFS, WorkspacePathError, workspace_for

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.permission.types import PermissionPolicy

GREP_OUTPUT_MODES: tuple[GrepOutputMode, ...] = ("content", "files_with_matches", "count")

#: 三个上下文参数名（``context`` 同时设前后文，单侧参数优先）
_CONTEXT_KEYS = ("context", "context_before", "context_after")


def _bad_args(message: str) -> ToolResult:
    """参数错误的统一形状。"""
    return ToolResult.error(f"bad_args: {message}", reason="bad_args")


def _flag(args: dict[str, Any], key: str) -> bool | ToolResult:
    """读取布尔开关参数；缺省即 schema 声明的 false（不是兜底），非布尔返回 bad_args。"""
    value = args.get(key, False)
    if not isinstance(value, bool):
        return _bad_args(f"{key} must be boolean")
    return value


def _parse_regex(args: dict[str, Any], *, multiline: bool) -> re.Pattern[str] | ToolResult:
    """校验 ``pattern`` / ``ignore_case`` 并编译正则；失败返回 bad_args 结果。

    ``multiline=True`` 时加 ``re.MULTILINE | re.DOTALL``：``^`` / ``$`` 匹配每行首尾，
    ``.`` 可匹配换行——跨行模式写 ``def f\\(.*?\\):`` 这类片段即可覆盖多行签名。
    """
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _bad_args("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        return _bad_args(f"pattern longer than {MAX_PATTERN_CHARS} chars")
    ignore_case = _flag(args, "ignore_case")
    if isinstance(ignore_case, ToolResult):
        return ignore_case
    flags = (re.IGNORECASE if ignore_case else 0) | (re.MULTILINE | re.DOTALL if multiline else 0)
    try:
        return re.compile(pattern, flags)
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


def _parse_context(
    args: dict[str, Any], *, mode: GrepOutputMode, multiline: bool, max_results: int,
) -> tuple[int, int] | ToolResult:
    """校验上下文参数，返回 ``(前文行数, 后文行数)``。

    ``context`` 同时设前后文，``context_before`` / ``context_after`` 单侧优先。非零上下文只在
    ``content`` 且非 multiline 时有意义，否则以 bad_args 明确拒绝（不静默忽略）；前后文之和
    必须小于 ``max_results``，保证至少一整组（前文 + 匹配行 + 后文）放得下。
    """
    raw = {key: args.get(key) for key in _CONTEXT_KEYS}
    for key, value in raw.items():
        if value is not None and not _is_nonneg_int(value):
            return _bad_args(f"{key} must be an integer >= 0")
    both = raw["context"] or 0
    before = both if raw["context_before"] is None else raw["context_before"]
    after = both if raw["context_after"] is None else raw["context_after"]
    if before == 0 and after == 0:
        return 0, 0
    if mode != "content":
        return _bad_args("context lines only apply to output_mode=content")
    if multiline:
        return _bad_args("context lines are not supported with multiline=true")
    if before + after >= max_results:
        return _bad_args(f"context_before + context_after must be < max_results ({max_results})")
    return before, after


def _parse_args(args: dict[str, Any], *, max_results: int) -> GrepQuery | ToolResult:
    """校验 LLM 参数并编译正则 / include；失败返回 bad_args 结果。"""
    multiline = _flag(args, "multiline")
    if isinstance(multiline, ToolResult):
        return multiline
    regex = _parse_regex(args, multiline=multiline)
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
    context = _parse_context(args, mode=mode, multiline=multiline, max_results=max_results)
    if isinstance(context, ToolResult):
        return context
    return GrepQuery(
        regex=regex, include=include, mode=mode, path=path,
        before=context[0], after=context[1], multiline=multiline,
    )


def _result_unit(query: GrepQuery) -> str:
    """截断尾注里的名额单位：随输出模式 / 上下文 / 跨行而不同。"""
    if query.mode != "content":
        return "files"
    if query.multiline:
        return "matches"
    return "output lines (matches + context)" if query.with_context else "matching lines"


def _render(run: GrepRun) -> ToolResult:
    """把收集结果渲染成 LLM 可见文本 + telemetry data。"""
    lines = list(run.hits) if run.hits else ["no matches found"]
    if run.truncated:
        lines.append(
            f"[results truncated at {run.limits.max_results} {_result_unit(run.query)}; "
            "narrow pattern / path / include to see the rest]"
        )
    lines.extend(skip_notes(run.stats, max_file_bytes=run.limits.max_file_bytes))
    return ToolResult.ok(
        "\n".join(lines),
        mode=run.query.mode,
        multiline=run.query.multiline,
        count=run.count,
        context_lines=run.context_lines,
        truncated=run.truncated,
        files_scanned=run.files_scanned,
        skipped_binary=run.stats.skipped_binary,
        skipped_large=len(run.stats.skipped_large),
        skipped_symlinks=run.stats.skipped_symlinks,
        skipped_ignored=run.stats.skipped_ignored,
        gitignore_unsupported=run.stats.gitignore_unsupported,
        unreadable=run.stats.unreadable,
    )


def _context_prop(side: str) -> dict[str, Any]:
    """上下文参数的 schema 片段。"""
    return {
        "type": "integer",
        "description": f"content 模式下每个匹配行{side}附带的上下文行数（缺省 0，占结果名额）",
    }


#: grep 的 input_schema（additionalProperties=false，内核派发前预校验）
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Python 正则（re 语法）；缺省逐行匹配，multiline=true 时整文件匹配",
        },
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
                "count=路径:匹配行数（multiline 时为匹配数）"
            ),
        },
        "context_before": _context_prop("之前"),
        "context_after": _context_prop("之后"),
        "context": _context_prop("前后各"),
        "multiline": {
            "type": "boolean",
            "description": (
                "整文件匹配，允许跨行（缺省 false）：^ / $ 匹配每行首尾，. 可匹配换行；"
                "content 输出 路径:起始行-结束行:\"匹配片段\""
            ),
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _description(root: str, limits: GrepLimits, respect_gitignore: bool) -> str:
    """LLM 可见描述：写明上限、跳过口径、行号语义与上下文 / 跨行用法。"""
    ignore_note = "遵循 .gitignore（被忽略的路径跳过并告知）；" if respect_gitignore else ""
    return (
        f"在沙盒（root={root}）内按正则搜索文件内容，纯只读。"
        f"结果最多 {limits.max_results} 条（content 模式按输出行计，上下文行也占名额；超出会截断"
        f"并提示收窄条件）；单行超过 {limits.max_line_chars} 字符截断；大于 "
        f"{limits.max_file_bytes} 字节的文件、二进制与非 UTF-8 文件跳过并在尾注告知。"
        f"{ignore_note}结果按路径排序，路径相对沙盒根、可直接用于 file_read；"
        "content 模式行号 1 基（file_read 的 offset = 行号 - 1），上下文行形如 路径-行号-行，"
        "不相邻的组以 -- 分隔；multiline=true 可匹配跨行片段。"
    )


def make_grep_tool(
    *,
    root_dir: str | Path | None = None,
    workspace: WorkspaceFS | None = None,
    policy: PermissionPolicy | None = None,
    max_results: int = 200,
    max_line_chars: int = 500,
    max_file_bytes: int = 2 * 1024 * 1024,
    exclude_dirs: frozenset[str] = DEFAULT_SEARCH_EXCLUDE_DIRS,
    respect_gitignore: bool = True,
    timeout_seconds: float = 30.0,
) -> ToolSpec:
    """构造 grep 工具（opt-in：经 ``EnginePool.create(extra_tools=[...])`` 注册）。

    Args:
        root_dir: 本机沙盒根；搜索基点与所有被读文件都必须落在其内（同 file_read）；与
            ``workspace`` 二选一。
        workspace: 注入的工作区（ADR 0113）；遍历与扫描逻辑不变，文件访问经它进行。
        policy: 可选权限策略；每次调用以 ``scope="file_read"``、target=搜索基点的规范路径审批一次。
        max_results: 结果名额（content 按输出行——匹配行与上下文行都占名额；其余按文件）；
            超出截断并告知。
        max_line_chars: content 模式单行 / multiline 单个匹配片段的字符上限。
        max_file_bytes: 单文件字节上限（默认 2MB）；更大的文件跳过并在尾注列出。
        exclude_dirs: 不下探的目录名集合（默认 ``DEFAULT_SEARCH_EXCLUDE_DIRS``）。
        respect_gitignore: 按沙盒内的 .gitignore 跳过路径（默认 True，理由见 ADR 0071）；
            跳过数量在尾注告知，显式指定的搜索基点自身不受影响。
        timeout_seconds: 单次调用超时（ToolSpec 级，超时由 runtime 统一处理）。

    Raises:
        ValueError: 上限参数非正；``root_dir`` 与 ``workspace`` 都给或都不给。
    """
    if min(max_results, max_line_chars, max_file_bytes) <= 0:
        raise ValueError("max_results / max_line_chars / max_file_bytes must be > 0")
    workspace = workspace_for(root_dir, workspace)
    root = workspace.root
    limits = GrepLimits(
        max_results=max_results, max_line_chars=max_line_chars, max_file_bytes=max_file_bytes,
    )

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """参数校验 → 沙盒 → 审批 → 工作线程遍历 → 渲染；取消返回 cancelled 结果。"""
        parsed = _parse_args(args, max_results=max_results)
        if isinstance(parsed, ToolResult):
            return parsed
        if ctx.cancel.is_cancelled:
            return cancelled_result(ctx)
        try:
            scope = search_scope(workspace, parsed.path)
        except WorkspacePathError:
            return ToolResult.error(
                f"path_outside_sandbox: {parsed.path} (root={root})", reason="sandbox_violation",
            )
        denied = await check_search_permission(
            policy, target=scope.target, ctx=ctx, tool_name="grep", pattern=parsed.regex.pattern,
        )
        if denied is not None:
            return denied
        try:
            exists = (await workspace.metadata(scope.target)).exists
        except OSError:
            exists = False
        if not exists:
            return ToolResult.error(f"not_found: {parsed.path}", reason="not_found")
        run = GrepRun(
            root=scope.root, fs=scope.fs, query=parsed, limits=limits,
            exclude_dirs=exclude_dirs, gitignore=respect_gitignore,
        )
        try:
            await run_in_worker(lambda stop: run.run(scope.base, stop), ctx.cancel)
        except SearchStopped:
            if ctx.cancel.is_cancelled:
                return cancelled_result(ctx)
            raise  # 停止信号只来自 token 取消或 await 被放弃，走到这里是内部契约被破坏
        return _render(run)

    return ToolSpec(
        name="grep",
        description=_description(root, limits, respect_gitignore),
        input_schema=_SCHEMA,
        handler=handler,
        parallel_safe=True,
        # 只读：崩溃恢复可安全重发，strict audit 记无副作用
        effect_kind="pure",
        reconciliation="none",
        timeout_seconds=timeout_seconds,
    )


__all__ = ["GREP_OUTPUT_MODES", "GrepOutputMode", "make_grep_tool"]
