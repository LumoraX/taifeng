"""apply_patch —— 结构化原子化补丁应用。

设计原则：
    - **结构化输入**：不解析 unified diff（parser 复杂、LLM 容易写错）；用
      三种 PatchSpec：edit / create / delete
    - **原子语义**：所有 patch 先 dry-run 全量校验，全过才执行；任一失败
      → 0 文件被修改
    - **工作区路径**：与 file_read / file_write 同语义；文件访问全部经 ``WorkspaceFS``
      （``root_dir`` 本机目录或注入的工作区，ADR 0113）
    - **可选权限**：效果模型（ADR 0028 / 0073）——每个被改动的路径各发一条
      ``file_write`` 请求（target = 解析后绝对路径），任一被拒则整组不执行

不支持（spec Non-goal）：
    - unified diff 格式（业务侧自己 wrap）
    - 文件 rename（用 delete + create 组合）
    - 二进制 / 权限位修改

详见 spec ``tool-builtins-extended``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from taifeng.tool.workspace import WorkspaceFS, WorkspacePathError, workspace_for

if TYPE_CHECKING:
    from pathlib import Path

# 每个 PatchSpec 的"类型"标记（互斥）
_PATCH_KIND_EDIT = "edit"
_PATCH_KIND_CREATE = "create"
_PATCH_KIND_DELETE = "delete"


def _classify_patch(p: dict[str, Any]) -> str | None:
    """识别 PatchSpec 类型，互斥校验。

    返回 ``"edit"`` / ``"create"`` / ``"delete"`` 或 None（非法 schema）。
    """
    is_create = bool(p.get("create"))
    is_delete = bool(p.get("delete"))
    has_old = "old_text" in p
    has_new = "new_text" in p

    if is_create and is_delete:
        return None
    if is_create:
        # create 类必须有 new_text；不应有 old_text
        if not has_new or has_old:
            return None
        return _PATCH_KIND_CREATE
    if is_delete:
        # delete 类不应有 old_text / new_text
        if has_old or has_new:
            return None
        return _PATCH_KIND_DELETE
    # edit 类必须 old_text + new_text 都有
    if has_old and has_new:
        return _PATCH_KIND_EDIT
    return None


def _resolve_patch(
    p: dict[str, Any], fs: WorkspaceFS,
) -> tuple[str | None, str, str | None]:
    """解析单条 patch 的类型与工作区内的规范路径；**不读文件内容**。

    权限审批需要解析后的路径，而审批必须先于任何内容读取（否则被拒的请求
    仍能从报错里探出目标文件的内容特征），故路径解析单独成步。

    返回 ``(resolved_path | None, kind, error_or_none)``。
    """
    path_str = p.get("path")
    if not isinstance(path_str, str) or not path_str:
        return None, "?", "missing_or_invalid_path"

    kind = _classify_patch(p)
    if kind is None:
        return None, "?", (
            "invalid_patch_spec: must be exactly one of "
            "edit (old_text+new_text) / create (new_text+create=true) / "
            "delete (delete=true)"
        )

    try:
        resolved = fs.resolve(path_str)
    except WorkspacePathError:
        return None, kind, f"sandbox_violation: {path_str}"
    return resolved, kind, None


async def _check_patch_content(
    fs: WorkspaceFS, resolved: str, kind: str, p: dict[str, Any],
) -> str | None:
    """phase 1 dry-run 内容校验；调用方保证路径已解析且审批已通过。

    返回错误描述，校验通过返回 None。
    """
    path_str = p["path"]
    try:
        info = await fs.metadata(resolved)
    except OSError as e:
        return f"read_error: {e}"
    if kind == _PATCH_KIND_EDIT:
        if not info.is_file:
            return f"path_not_found: {path_str}"
        try:
            content = (await fs.read_bytes(resolved)).decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"read_error: {e}"
        old_text = p["old_text"]
        if not isinstance(old_text, str):
            return "old_text_must_be_string"
        occurrences = content.count(old_text)
        if occurrences == 0:
            return f"old_text_not_found: {path_str}"
        if occurrences > 1:
            return f"ambiguous_old_text: {path_str} (occurrences={occurrences})"
        if not isinstance(p.get("new_text"), str):
            return "new_text_must_be_string"
    elif kind == _PATCH_KIND_CREATE:
        if info.exists:
            return f"path_exists: {path_str}"
        if not isinstance(p.get("new_text"), str):
            return "new_text_must_be_string"
    elif kind == _PATCH_KIND_DELETE:
        if not info.exists:
            return f"path_not_found: {path_str}"
        # 目录不能当文件删：留到应用阶段才失败的话，排在它前面的 patch 已经落盘
        if info.is_directory:
            return f"not_a_file: {path_str}"
    return None


def _validation_error(index: int, kind: str, err: str) -> ToolResult:
    """统一构造 ``patch_validation_failed`` 错误结果。"""
    return ToolResult.error(
        f"patch_validation_failed: patches[{index}] {err}",
        reason="patch_validation_failed",
        patch_index=index,
        patch_kind=kind,
        error=err,
    )


def _group_kinds_by_path(
    resolved: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, list[str]]:
    """按路径聚合 patch 类型，保持路径首次出现的顺序。"""
    grouped: dict[str, list[str]] = {}
    for path, kind, _ in resolved:
        grouped.setdefault(path, []).append(kind)
    return grouped


async def _check_write_permissions(
    policy: PermissionPolicy,
    resolved: list[tuple[str, str, dict[str, Any]]],
    ctx: ToolContext,
) -> ToolResult | None:
    """逐路径发 ``file_write`` 审批；首个被拒即返回错误结果，全过返回 None。

    同一路径的多条 patch 只审批一次。遇拒即停：整组已注定不执行，继续为
    其余路径打扰审批人没有意义。
    """
    for path, kinds in _group_kinds_by_path(resolved).items():
        req = PermissionRequest(
            scope="file_write",
            target=path,
            reason="LLM 请求以结构化补丁改动文件",
            metadata={
                "tool": "apply_patch",
                "patch_kinds": kinds,
                "patch_count": len(resolved),
                "thread_id": ctx.thread_id,
                "call_id": ctx.call_id,
                "submission_id": ctx.extras.get("submission_id"),
            },
        )
        decision = await policy.check(req)
        if not decision.granted:
            return ToolResult.error(
                f"permission_denied: {decision.reason}",
                reason="permission_denied",
                denied_path=path,
            )
    return None


async def _apply_one(fs: WorkspaceFS, resolved: str, kind: str, p: dict[str, Any]) -> None:
    """phase 2 apply 单条 patch；调用方保证 phase 1 已通过。"""
    if kind == _PATCH_KIND_EDIT:
        content = (await fs.read_bytes(resolved)).decode("utf-8")
        new_content = content.replace(p["old_text"], p["new_text"], 1)
        await fs.write_bytes(resolved, new_content.encode("utf-8"), create_parents=False)
    elif kind == _PATCH_KIND_CREATE:
        await fs.write_bytes(resolved, p["new_text"].encode("utf-8"), create_parents=True)
    elif kind == _PATCH_KIND_DELETE:
        await fs.remove(resolved)


def make_apply_patch_tool(
    *,
    root_dir: str | Path | None = None,
    workspace: WorkspaceFS | None = None,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
) -> ToolSpec:
    """构造 apply_patch 工具。

    Args:
        root_dir: 本机沙盒根目录；所有 patch 的 path 必须落在此目录下；与 ``workspace`` 二选一
        workspace: 注入的工作区（容器 / 远端沙盒里的文件系统，ADR 0113）
        policy: 可选权限策略；非 None 时每个被改动的路径发一条 ``file_write``
            审批（target = 工作区内的规范路径），任一被拒则整组不执行
        max_bytes: 单个 patch 的 new_text 字节上限（防止 LLM 大段贴）

    Raises:
        ValueError: ``root_dir`` 与 ``workspace`` 都给或都不给。
    """
    fs = workspace_for(root_dir, workspace)

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        patches = args.get("patches")
        if not isinstance(patches, list) or not patches:
            return ToolResult.error(
                "bad_args: patches must be a non-empty list",
                reason="bad_args",
            )

        # 单 patch 大小预校验
        for i, p in enumerate(patches):
            if not isinstance(p, dict):
                return ToolResult.error(
                    f"bad_args: patches[{i}] must be object",
                    reason="bad_args",
                )
            new_text = p.get("new_text", "")
            if (
                isinstance(new_text, str)
                and len(new_text.encode("utf-8")) > max_bytes
            ):
                return ToolResult.error(
                    f"too_large: patches[{i}] new_text exceeds {max_bytes} bytes",
                    reason="too_large",
                )

        # 先做纯路径解析（不读内容）：审批需要解析后的绝对路径
        resolved_patches: list[tuple[str, str, dict[str, Any]]] = []
        for i, p in enumerate(patches):
            resolved, kind, err = _resolve_patch(p, fs)
            if err is not None or resolved is None:
                return _validation_error(i, kind, err or "unresolved_path")
            resolved_patches.append((resolved, kind, p))

        # 按路径逐个审批（效果模型：写 / 删文件 → file_write + 绝对路径）
        if policy is not None:
            denied = await _check_write_permissions(policy, resolved_patches, ctx)
            if denied is not None:
                return denied

        # phase 1: dry-run 内容校验（审批通过后才读文件）
        for i, (resolved, kind, p) in enumerate(resolved_patches):
            content_err = await _check_patch_content(fs, resolved, kind, p)
            if content_err is not None:
                return _validation_error(i, kind, content_err)
        validated = resolved_patches

        # phase 2: 实际应用（phase 1 全过才走到这里）
        applied: list[dict[str, Any]] = []
        try:
            for resolved, kind, p in validated:
                await _apply_one(fs, resolved, kind, p)
                applied.append({
                    "path": resolved,
                    "kind": kind,
                })
        except OSError as e:
            # phase 2 偶发 IO 失败（磁盘满 / 权限等）—— 此时部分文件已改，
            # 报告但不尝试回滚（业务自己用 store 重放或重跑）
            return ToolResult.error(
                f"apply_io_error: {e} (applied={len(applied)} of "
                f"{len(validated)})",
                reason="apply_io_error",
                applied_count=len(applied),
                total_count=len(validated),
            )

        return ToolResult.ok(
            f"applied {len(applied)} patch(es)",
            applied=applied,
        )

    return ToolSpec(
        name="apply_patch",
        description=(
            "Apply a list of structured patches atomically. "
            "Each patch is one of: "
            "edit (path+old_text+new_text), "
            "create (path+new_text+create=true), "
            "delete (path+delete=true). "
            "All patches are dry-run validated first; "
            "if any fails, no files are modified."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "相对于沙盒根的路径",
                            },
                            "old_text": {
                                "type": "string",
                                "description": (
                                    "edit 模式：要替换的旧文本（必须在文件中恰好出现 1 次）"
                                ),
                            },
                            "new_text": {
                                "type": "string",
                                "description": (
                                    "edit / create 模式：新文本"
                                ),
                            },
                            "create": {
                                "type": "boolean",
                                "description": "create 模式标记；path 必须不存在",
                            },
                            "delete": {
                                "type": "boolean",
                                "description": "delete 模式标记；path 必须存在",
                            },
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["patches"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=False,
        # 打补丁改文件是外部不可幂等副作用，恢复需人工核对
        effect_kind="external_non_idempotent",
        reconciliation="manual",
        timeout_seconds=30.0,
    )
