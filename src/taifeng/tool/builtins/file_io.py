"""file_read / file_write —— 受 PermissionPolicy 约束的文件 IO 工具。

设计：
    - 工作区（``root_dir`` 本机目录，或注入的 ``WorkspaceFS``，ADR 0113）：所有路径必须落在
      工作区之内，否则拒绝；文件访问全部经 ``WorkspaceFS``
    - PermissionPolicy（可选）：业务侧可注入审批策略
    - file_read 默认 ``parallel_safe=True``；file_write ``parallel_safe=False``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.llm.image_input import ImageAttachmentV1
from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from taifeng.tool.workspace import WorkspaceFS, WorkspacePathError, resolve_local, workspace_for

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.types import ImageMediaType

# 本机路径解析的旧名（搜索工具与既有测试沿用）
_resolve_safe = resolve_local


def _sniff_image(data: bytes) -> ImageMediaType | None:
    """按文件头判断图片类型（PNG / JPEG / GIF / WebP）；不是图片返回 None。

    只看内容不看扩展名：扩展名由写文件的一方决定，不可信。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_result(
    data: bytes,
    media_type: ImageMediaType,
    rel: str,
    resolved: str,
    *,
    attach: bool,
    max_bytes: int,
) -> ToolResult:
    """图片文件的读取结果：开启附图时把图片作为附件返回，否则如实说明读不了。

    图片不能当文本读（解码必然失败或得到乱码）；返回附件后，是否收得下由宿主的
    ``ImageInputPolicy`` 在落盘前准入（tool-image-attachment 契约），本工具只管自己的
    读取上限。
    """
    if not attach:
        return ToolResult.error(
            f"image_not_attached: {rel} is an image ({media_type}, {len(data)} bytes); "
            "this file_read is not configured to return images",
            reason="image_not_attached",
        )
    if len(data) > max_bytes:
        return ToolResult.error(
            f"too_large: image is {len(data)} bytes, max {max_bytes}", reason="too_large",
        )
    attachment = ImageAttachmentV1.from_bytes(data, media_type=media_type)
    return ToolResult.ok(
        # 文本里只写调用方给的相对路径：宿主绝对路径不进模型上下文
        f"[image {rel}: {media_type}, {len(data)} bytes, attached]",
        attachments=(attachment,),
        path=resolved,
        bytes=len(data),
    )


def _text_result(
    raw: str, offset: int | None, limit: int | None, resolved: str, *, max_bytes: int,
) -> ToolResult:
    """文本文件的读取结果：按是否分页决定截断方式。"""
    if offset is not None or limit is not None:
        # 分页读：先按行切片再对结果限幅——绕过整文件 byte-cap，
        # 否则大文件(如 offload 落盘结果)会被先截断到 max_bytes，后段行不可达。
        content = _paginate_lines(raw, offset, limit)
        truncated = len(content.encode("utf-8")) > max_bytes
        if truncated:
            content = content[:max_bytes]
    else:
        # 整文件读：保持旧行为(按字节阈值判定 + 字符切片截断)
        truncated = len(raw.encode("utf-8")) > max_bytes
        content = raw[:max_bytes] if truncated else raw
    suffix = f"\n\n[truncated to {max_bytes} bytes]" if truncated else ""
    return ToolResult.ok(content + suffix, path=resolved, bytes=len(content))


def _is_nonneg_int(value: Any) -> bool:
    """是否为非负整数（排除 bool，bool 是 int 子类需显式剔除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _paginate_lines(raw: str, offset: int | None, limit: int | None) -> str:
    """按行分页:从 ``offset`` 行起取 ``limit`` 行(limit 省略则到末尾)。

    用 ``splitlines()`` 切行(不产生尾随空行,且与 stub 预览行号语义一致)。
    offset 越界时切片自然返回空串,不报错——便于 LLM 探测分页边界。
    """
    lines = raw.splitlines()
    start = offset or 0
    end = len(lines) if limit is None else start + limit
    return "\n".join(lines[start:end])


def make_file_read_tool(
    *,
    root_dir: str | Path | None = None,
    workspace: WorkspaceFS | None = None,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
    attach_images: bool = False,
    max_image_bytes: int = 10 * 1024 * 1024,
) -> ToolSpec:
    """文件读取工具。

    Args:
        root_dir: 本机沙盒根。路径只能在该目录下；与 ``workspace`` 二选一
        workspace: 注入的工作区（容器 / 远端沙盒里的文件系统，ADR 0113）
        policy: 可选权限策略（拒绝时 LLM 收到 error）
        max_bytes: 单次读取上限（默认 1MB）
        attach_images: 读到图片文件（PNG / JPEG / GIF / WebP，按文件头识别）时是否作为
            图片附件返回给模型。默认 False：图片文件返回 ``image_not_attached`` 错误。
            开启后还需宿主注入 enabled 的 ``ImageInputPolicy``，否则该次调用以
            ``tool_attachment_rejected`` 判错（与 MCP 桥的 ``attach_images`` 同义，ADR 0147）。
        max_image_bytes: 单张图片的读取上限（默认 10MB，与 ``ImageInputPolicy`` 默认一致）

    Raises:
        ValueError: ``root_dir`` 与 ``workspace`` 都给或都不给；``max_image_bytes`` 非正。
    """
    if max_image_bytes <= 0:
        raise ValueError(f"max_image_bytes must be positive, got {max_image_bytes!r}")
    fs = workspace_for(root_dir, workspace)
    root = fs.root

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        if not rel or not isinstance(rel, str):
            return ToolResult.error("bad_args: path required", reason="bad_args")
        try:
            resolved = fs.resolve(rel)
        except WorkspacePathError:
            return ToolResult.error(
                f"path_outside_sandbox: {rel} (root={root})",
                reason="sandbox_violation",
            )
        if policy is not None:
            req = PermissionRequest(
                scope="file_read",
                target=resolved,
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
        try:
            info = await fs.metadata(resolved)
        except OSError as e:
            return ToolResult.error(f"read_error: {e}", reason="read_error")
        if not info.is_file:
            return ToolResult.error(f"not_a_file: {resolved}", reason="not_found")
        # 分页参数校验：offset/limit 可选，给定则必须为非负整数
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None and not _is_nonneg_int(offset):
            return ToolResult.error("bad_args: offset must be int >= 0", reason="bad_args")
        if limit is not None and not _is_nonneg_int(limit):
            return ToolResult.error("bad_args: limit must be int >= 0", reason="bad_args")
        try:
            data = await fs.read_bytes(resolved)
        except OSError as e:
            return ToolResult.error(f"read_error: {e}", reason="read_error")
        media_type = _sniff_image(data)
        if media_type is not None:
            return _image_result(
                data, media_type, rel, resolved,
                attach=attach_images, max_bytes=max_image_bytes,
            )
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError as e:
            return ToolResult.error(f"read_error: {e}", reason="read_error")
        return _text_result(raw, offset, limit, resolved, max_bytes=max_bytes)

    return ToolSpec(
        name="file_read",
        description=(
            f"读取沙盒（root={root}）内的文本文件。单页最大 {max_bytes} 字节。"
            "可选 offset/limit 按行分页读大文件（如被 offload 落盘的工具结果），"
            "避免整文件截断。"
            + (
                "图片文件（PNG / JPEG / GIF / WebP）会作为图片返回，可用来重新查看"
                "已从上下文移出、落盘在 _offload/ 下的图片。"
                if attach_images else ""
            )
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于沙盒根的路径"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "起始行号（0 基，省略=从头读）",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "读取行数（省略=读到文件末尾）",
                },
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
    root_dir: str | Path | None = None,
    workspace: WorkspaceFS | None = None,
    policy: PermissionPolicy | None = None,
    max_bytes: int = 1024 * 1024,
    create_dirs: bool = True,
) -> ToolSpec:
    """文件写入工具（本机工作区是原子写：写临时文件 + 原子 rename）。

    ``root_dir`` 与 ``workspace`` 二选一（ADR 0113）；注入的工作区是否原子写由其实现决定。

    Raises:
        ValueError: ``root_dir`` 与 ``workspace`` 都给或都不给。
    """
    fs = workspace_for(root_dir, workspace)
    root = fs.root

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
        try:
            resolved = fs.resolve(rel)
        except WorkspacePathError:
            return ToolResult.error(
                f"path_outside_sandbox: {rel}", reason="sandbox_violation",
            )
        if policy is not None:
            req = PermissionRequest(
                scope="file_write",
                target=resolved,
                reason="LLM 请求写入文件",
                metadata={"bytes": len(content), "thread_id": ctx.thread_id},
            )
            decision = await policy.check(req)
            if not decision.granted:
                return ToolResult.error(
                    f"permission_denied: {decision.reason}", reason="permission_denied",
                )
        try:
            await fs.write_bytes(resolved, content.encode("utf-8"), create_parents=create_dirs)
        except OSError as e:
            return ToolResult.error(f"write_error: {e}", reason="write_error")
        return ToolResult.ok(f"wrote {len(content)} chars to {resolved}", path=resolved)

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
        # 写文件是外部不可幂等副作用，恢复需人工核对（不自动重试）
        effect_kind="external_non_idempotent",
        reconciliation="manual",
        timeout_seconds=10.0,
    )
