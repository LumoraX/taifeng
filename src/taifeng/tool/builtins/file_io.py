"""file_read / file_write —— 受 PermissionPolicy 约束的文件 IO 工具。

设计：
    - 根目录沙盒（root_dir）：所有路径必须落在 root_dir 之下，否则拒绝
    - PermissionPolicy（可选）：业务侧可注入审批策略
    - file_read 默认 ``parallel_safe=True``；file_write ``parallel_safe=False``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


def _resolve_safe(root: Path, requested: str) -> Path | None:
    """解析路径；落在 root 外返回 None。"""
    try:
        p = (root / requested).resolve(strict=False)
        if root not in p.parents and p != root:
            return None
        return p
    except OSError:
        return None


def make_file_read_tool(
    *,
    root_dir: str | Path,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
) -> ToolSpec:
    """文件读取工具。

    Args:
        root_dir: 沙盒根。路径只能在该目录下
        policy: 可选权限策略（拒绝时 LLM 收到 error）
        max_bytes: 单次读取上限（默认 1MB）
    """
    root = Path(root_dir).expanduser().resolve()

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        if not rel or not isinstance(rel, str):
            return ToolResult.error("bad_args: path required", reason="bad_args")
        resolved = _resolve_safe(root, rel)
        if resolved is None:
            return ToolResult.error(
                f"path_outside_sandbox: {rel} (root={root})",
                reason="sandbox_violation",
            )
        if policy is not None:
            req = PermissionRequest(
                scope="file_read",
                target=str(resolved),
                reason="LLM 请求读取文件",
                metadata={
                    "thread_id": ctx.thread_id,
                    "call_id": ctx.call_id,
                    "submission_id": ctx.extras.get("submission_id"),
                },
            )
            decision = await policy.check(req)
            if not decision.granted:
                return ToolResult.error(
                    f"permission_denied: {decision.reason}", reason="permission_denied",
                )
        if not resolved.is_file():
            return ToolResult.error(f"not_a_file: {resolved}", reason="not_found")
        try:
            size = resolved.stat().st_size
            if size > max_bytes:
                content = resolved.read_text(encoding="utf-8")[:max_bytes]
                truncated = True
            else:
                content = resolved.read_text(encoding="utf-8")
                truncated = False
        except (OSError, UnicodeDecodeError) as e:
            return ToolResult.error(f"read_error: {e}", reason="read_error")
        suffix = f"\n\n[truncated to {max_bytes} bytes]" if truncated else ""
        return ToolResult.ok(content + suffix, path=str(resolved), bytes=len(content))

    return ToolSpec(
        name="file_read",
        description=f"读取沙盒（root={root}）内的文本文件。最大 {max_bytes} 字节。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于沙盒根的路径"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,
        timeout_seconds=10.0,
    )


def make_file_write_tool(
    *,
    root_dir: str | Path,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
    create_dirs: bool = True,
) -> ToolSpec:
    """文件写入工具（默认 atomic：写临时文件 + 原子 rename）。"""
    root = Path(root_dir).expanduser().resolve()

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        content = args.get("content", "")
        if not rel or not isinstance(rel, str):
            return ToolResult.error("bad_args: path required", reason="bad_args")
        if not isinstance(content, str):
            return ToolResult.error("bad_args: content must be string", reason="bad_args")
        if len(content.encode("utf-8")) > max_bytes:
            return ToolResult.error(
                f"too_large: max {max_bytes} bytes", reason="too_large",
            )
        resolved = _resolve_safe(root, rel)
        if resolved is None:
            return ToolResult.error(
                f"path_outside_sandbox: {rel}", reason="sandbox_violation",
            )
        if policy is not None:
            req = PermissionRequest(
                scope="file_write",
                target=str(resolved),
                reason="LLM 请求写入文件",
                metadata={"bytes": len(content), "thread_id": ctx.thread_id},
            )
            decision = await policy.check(req)
            if not decision.granted:
                return ToolResult.error(
                    f"permission_denied: {decision.reason}", reason="permission_denied",
                )
        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, resolved)
        except OSError as e:
            return ToolResult.error(f"write_error: {e}", reason="write_error")
        return ToolResult.ok(f"wrote {len(content)} chars to {resolved}", path=str(resolved))

    return ToolSpec(
        name="file_write",
        description=f"原子写入沙盒（root={root}）内的文本文件。最大 {max_bytes} 字节。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于沙盒根的路径"},
                "content": {"type": "string", "description": "写入内容（UTF-8）"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=False,
        timeout_seconds=10.0,
    )
