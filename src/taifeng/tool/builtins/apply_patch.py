"""apply_patch —— 结构化原子化补丁应用。

设计原则：
    - **结构化输入**：不解析 unified diff（parser 复杂、LLM 容易写错）；用
      三种 PatchSpec：edit / create / delete
    - **原子语义**：所有 patch 先 dry-run 全量校验，全过才执行；任一失败
      → 0 文件被修改
    - **沙盒路径**：复用 ``file_io._resolve_safe`` 同语义
    - **可选权限**：整组 patch 一次 ``PermissionPolicy.check``（不是每个）

不支持（spec Non-goal）：
    - unified diff 格式（业务侧自己 wrap）
    - 文件 rename（用 delete + create 组合）
    - 二进制 / 权限位修改

详见 spec ``tool-builtins-extended``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.builtins.file_io import _resolve_safe
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

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


def _validate_patch(
    p: dict[str, Any], root: Path,
) -> tuple[Path | None, str, str | None]:
    """phase 1 dry-run 校验单条 patch。

    返回 ``(resolved_path | None, kind, error_or_none)``：
        - 校验通过 → (path, kind, None)
        - 失败 → (None, kind or "?", error_message)
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

    resolved = _resolve_safe(root, path_str)
    if resolved is None:
        return None, kind, f"sandbox_violation: {path_str}"

    if kind == _PATCH_KIND_EDIT:
        if not resolved.is_file():
            return None, kind, f"path_not_found: {path_str}"
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return None, kind, f"read_error: {e}"
        old_text = p["old_text"]
        if not isinstance(old_text, str):
            return None, kind, "old_text_must_be_string"
        occurrences = content.count(old_text)
        if occurrences == 0:
            return None, kind, f"old_text_not_found: {path_str}"
        if occurrences > 1:
            return None, kind, (
                f"ambiguous_old_text: {path_str} (occurrences={occurrences})"
            )
        if not isinstance(p.get("new_text"), str):
            return None, kind, "new_text_must_be_string"
    elif kind == _PATCH_KIND_CREATE:
        if resolved.exists():
            return None, kind, f"path_exists: {path_str}"
        if not isinstance(p.get("new_text"), str):
            return None, kind, "new_text_must_be_string"
    elif kind == _PATCH_KIND_DELETE:
        if not resolved.exists():
            return None, kind, f"path_not_found: {path_str}"

    return resolved, kind, None


def _apply_one(resolved: Path, kind: str, p: dict[str, Any]) -> None:
    """phase 2 apply 单条 patch；调用方保证 phase 1 已通过。"""
    if kind == _PATCH_KIND_EDIT:
        content = resolved.read_text(encoding="utf-8")
        new_content = content.replace(p["old_text"], p["new_text"], 1)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, resolved)
    elif kind == _PATCH_KIND_CREATE:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(p["new_text"], encoding="utf-8")
        os.replace(tmp, resolved)
    elif kind == _PATCH_KIND_DELETE:
        resolved.unlink()


def make_apply_patch_tool(
    *,
    root_dir: str | Path,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
) -> ToolSpec:
    """构造 apply_patch 工具。

    Args:
        root_dir: 沙盒根目录；所有 patch 的 path 必须落在此目录下
        policy: 可选权限策略；非 None 时整组 patch 调一次 check
        max_bytes: 单个 patch 的 new_text 字节上限（防止 LLM 大段贴）
    """
    root = Path(root_dir).expanduser().resolve()

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

        # 整组一次 permission 审批（spec: apply_patch 是一组原子操作）
        if policy is not None:
            req = PermissionRequest(
                scope="tool_use",
                target="apply_patch",
                reason="LLM 请求应用一组结构化补丁",
                metadata={
                    "patch_count": len(patches),
                    "thread_id": ctx.thread_id,
                    "call_id": ctx.call_id,
                },
            )
            decision = await policy.check(req)
            if not decision.granted:
                return ToolResult.error(
                    f"permission_denied: {decision.reason}",
                    reason="permission_denied",
                )

        # phase 1: dry-run 全量校验
        validated: list[tuple[Path, str, dict[str, Any]]] = []
        for i, p in enumerate(patches):
            resolved, kind, err = _validate_patch(p, root)
            if err is not None or resolved is None:
                return ToolResult.error(
                    f"patch_validation_failed: patches[{i}] {err}",
                    reason="patch_validation_failed",
                    patch_index=i,
                    patch_kind=kind,
                    error=err,
                )
            validated.append((resolved, kind, p))

        # phase 2: 实际应用（phase 1 全过才走到这里）
        applied: list[dict[str, Any]] = []
        try:
            for resolved, kind, p in validated:
                _apply_one(resolved, kind, p)
                applied.append({
                    "path": str(resolved),
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
