"""glob —— 沙盒内按 glob 模式列文件（opt-in 内置工具，ADR 0064）。

设计要点：
    - 模式相对搜索基点（``path``，缺省=沙盒根），标准 glob 语义：``*.py`` 只看基点这一层，
      递归用 ``**/*.py``；支持 ``?`` / ``[...]`` / ``{a,b}``，大小写敏感
    - 结果按路径逐段字典序排列（确定性；理由见 ``search_walk`` 模块说明与 ADR 0064），
      ``max_results`` 截断时在输出尾明确告知，且遍历提前停止
    - 只列文件（目录本身不作为结果）；输出路径相对沙盒根，可直接喂给 file_read
    - 沙盒 / 符号链接 / 权限 / 取消语义见 ``search_walk``；``parallel_safe=True``、``pure``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from taifeng.tool.builtins.file_io import _resolve_safe
from taifeng.tool.builtins.search_walk import (
    DEFAULT_SEARCH_EXCLUDE_DIRS,
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


@dataclass
class _GlobRun:
    """工作线程内的一次 glob：遍历基点、匹配、按上限收集。"""

    root: Path
    matcher: GlobMatcher
    max_results: int
    exclude_dirs: frozenset[str]
    hits: list[str] = field(default_factory=list)
    truncated: bool = False
    stats: WalkStats = field(default_factory=WalkStats)

    def run(self, base: Path, should_stop: Callable[[], bool]) -> None:
        """收集匹配文件；第 ``max_results + 1`` 个命中时标记截断并停止遍历。

        Raises:
            SearchStopped: 取消 / 超时的停止信号。
        """
        files = iter_files(
            base, root=self.root, exclude_dirs=self.exclude_dirs,
            should_stop=should_stop, stats=self.stats,
        )
        for path in files:
            if not self.matcher.matches(path.relative_to(base).parts):
                continue
            if len(self.hits) >= self.max_results:
                self.truncated = True
                return
            self.hits.append(rel_to_root(path, self.root))


def _render(run: _GlobRun) -> ToolResult:
    """渲染 LLM 可见文本（每行一个路径 + 尾注）与 telemetry data。"""
    lines = list(run.hits) if run.hits else ["no files matched"]
    if run.truncated:
        lines.append(
            f"[results truncated at {run.max_results} files; "
            "narrow the pattern or path to see the rest]"
        )
    lines.extend(skip_notes(run.stats))
    return ToolResult.ok(
        "\n".join(lines),
        count=len(run.hits),
        truncated=run.truncated,
        skipped_symlinks=run.stats.skipped_symlinks,
        unreadable=run.stats.unreadable,
    )


def _parse_args(args: dict[str, Any]) -> tuple[GlobMatcher, str] | ToolResult:
    """校验 ``pattern`` / ``path``；返回 (编译后的匹配器, 基点相对路径) 或 bad_args 结果。"""
    pattern = args.get("pattern")
    if not isinstance(pattern, str):
        return ToolResult.error("bad_args: pattern must be string", reason="bad_args")
    # path 缺省即 schema 声明的「沙盒根」
    path = args.get("path", ".")
    if not isinstance(path, str):
        return ToolResult.error("bad_args: path must be string", reason="bad_args")
    try:
        matcher = compile_glob(pattern)
    except ValueError as exc:
        return ToolResult.error(f"bad_args: {exc}", reason="bad_args")
    return matcher, path


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "glob 模式，相对搜索基点，如 **/*.py、src/*.{ts,tsx}",
        },
        "path": {
            "type": "string",
            "description": "搜索基点：相对沙盒根的目录（缺省=沙盒根）",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def make_glob_tool(
    *,
    root_dir: str | Path,
    policy: PermissionPolicy | None = None,
    max_results: int = 200,
    exclude_dirs: frozenset[str] = DEFAULT_SEARCH_EXCLUDE_DIRS,
    timeout_seconds: float = 30.0,
) -> ToolSpec:
    """构造 glob 工具（opt-in：经 ``EnginePool.create(extra_tools=[...])`` 注册）。

    Args:
        root_dir: 沙盒根；搜索基点必须落在其内（同 file_read 的 ``_resolve_safe``）。
        policy: 可选权限策略；每次调用以 ``scope="file_read"``、target=基点绝对路径审批一次。
        max_results: 结果条数上限；超出截断并在输出尾告知。
        exclude_dirs: 不下探的目录名集合（默认 ``DEFAULT_SEARCH_EXCLUDE_DIRS``）。
        timeout_seconds: 单次调用超时（ToolSpec 级，超时由 runtime 统一处理）。

    Raises:
        ValueError: ``max_results`` 非正。
    """
    if max_results <= 0:
        raise ValueError("max_results must be > 0")
    root = Path(root_dir).expanduser().resolve()

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = _parse_args(args)
        if isinstance(parsed, ToolResult):
            return parsed
        matcher, rel = parsed
        if ctx.cancel.is_cancelled:
            return cancelled_result(ctx)
        base = _resolve_safe(root, rel)
        if base is None:
            return ToolResult.error(
                f"path_outside_sandbox: {rel} (root={root})", reason="sandbox_violation",
            )
        denied = await check_search_permission(
            policy, target=base, ctx=ctx, tool_name="glob", pattern=args["pattern"],
        )
        if denied is not None:
            return denied
        if not base.is_dir():
            return ToolResult.error(f"not_a_directory: {rel}", reason="not_found")
        run = _GlobRun(
            root=root, matcher=matcher, max_results=max_results, exclude_dirs=exclude_dirs,
        )
        try:
            await run_in_worker(lambda stop: run.run(base, stop), ctx.cancel)
        except SearchStopped:
            if ctx.cancel.is_cancelled:
                return cancelled_result(ctx)
            raise  # 停止信号只来自 token 取消或 await 被放弃，走到这里是内部契约被破坏
        return _render(run)

    return ToolSpec(
        name="glob",
        description=(
            f"在沙盒（root={root}）内按 glob 模式列出文件，纯只读。模式相对搜索基点："
            "*.py 只匹配基点这一层，递归用 **/*.py；支持 ? [...] {a,b}，大小写敏感。"
            f"结果按路径排序，最多 {max_results} 条（超出会截断并提示收窄条件）；"
            "路径相对沙盒根，可直接用于 file_read。"
        ),
        input_schema=_SCHEMA,
        handler=handler,
        parallel_safe=True,
        # 只读：崩溃恢复可安全重发，strict audit 记无副作用
        effect_kind="pure",
        reconciliation="none",
        timeout_seconds=timeout_seconds,
    )


__all__ = ["make_glob_tool"]
