"""MCP ``tools/call`` 结果 → 内核工具结果的无损投影（MCP 2025-06-18 §Tools「Tool Result」）。

旧实现（``extract_text_content`` 的前身）把 image 降级成 ``[image: mime]``、整个丢掉
``structuredContent``、把其余类型 ``json.dumps`` 进文本（含 base64 正文）。本模块按类型分流：

| MCP 内容 | 投影 |
| --- | --- |
| ``text`` | 原文进 ``text`` |
| ``image`` | ``attach_images=True`` → ``ImageAttachmentV1`` 附件（走 tool-image-attachment 契约的
  落盘前 admission）；``False`` → 带 MIME 与字节数的显式占位 |
| ``audio`` | 带 MIME 与字节数的显式占位（内核无音频模态） |
| ``resource``（embedded） | text 资源：标注 uri 后内联原文；blob 资源：带 MIME 与字节数的占位 |
| ``resource_link`` | 标注 name / uri / MIME / 描述的引用行 |
| ``structuredContent`` | 原对象进 ``structured_content``；文本侧若没有与之 JSON 等价的 text 块，
  补一段序列化 JSON（规范 SHOULD 由 server 做的向后兼容，server 没做时客户端补上） |

base64 正文**绝不**进文本（R3：与 tool-image-attachment 的脱敏口径一致）。形状不合法的内容
抛 ``McpContentError``——由桥转成该次工具调用的错误结果，不静默丢弃。

参照：codex ``codex-rs/protocol/src/models.rs`` 的
``CallToolResult::as_function_call_output_payload``（有 structuredContent 时模型只看它的
序列化、content 整体让位）与 ``convert_mcp_content_to_items``。
差异：taifeng 保留 content（图片走 ``ToolResult.attachments`` 契约位，文本仍是 ``output`` 权威
投影），structuredContent 只在 text 块里缺等价 JSON 时补进文本——两者都不丢。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from taifeng.llm.image_input import SUPPORTED_IMAGE_MEDIA_TYPES, ImageAttachmentV1

if TYPE_CHECKING:
    from taifeng.llm.types import ImageMediaType


class McpContentError(ValueError):
    """MCP 工具结果形状不合法（无法如实投影），由桥转为该次调用的错误结果。"""


@dataclass(frozen=True)
class McpToolOutput:
    """一次 ``tools/call`` 结果的内核投影。

    Attributes:
        text: 模型可见的权威文本投影（各内容块按出现顺序以换行拼接）。
        is_error: MCP ``isError``（工具执行错误，区别于 JSON-RPC 协议错误）。
        attachments: 按出现顺序的图片附件（``attach_images=False`` 时恒为空）。
        structured_content: MCP ``structuredContent`` 原对象；未提供为 None。
    """

    text: str
    is_error: bool
    attachments: tuple[ImageAttachmentV1, ...]
    structured_content: dict[str, Any] | None


def _require_str(item: dict[str, Any], key: str, where: str) -> str:
    """取必填字符串字段；缺失或类型不对抛 ``McpContentError``。"""
    value = item.get(key)
    if not isinstance(value, str):
        raise McpContentError(f"{where}: field {key!r} must be a string")
    return value


def _base64_size(data: str) -> int:
    """由 base64 文本长度推算解码字节数（占位文本用，不解码、不校验）。"""
    stripped = data.strip()
    padding = len(stripped) - len(stripped.rstrip("="))
    return max(0, len(stripped) * 3 // 4 - padding)


def _image_attachment(item: dict[str, Any], where: str) -> ImageAttachmentV1:
    """把 MCP image 块转成 canonical 附件；MIME 不受支持 / base64 非法 / 空图显式报错。

    这里只做**形状**校验（能否表达为 ``ImageAttachmentV1``）；数量 / 字节 / 尺寸 / 帧数等
    资源策略由 loop 在落盘前按宿主注入的 ``ImageInputPolicy`` 执行（tool-image-attachment
    契约「准入前置」），两处职责不重叠。
    """
    mime = _require_str(item, "mimeType", where)
    data = _require_str(item, "data", where)
    if mime not in SUPPORTED_IMAGE_MEDIA_TYPES:
        raise McpContentError(
            f"{where}: image mimeType {mime!r} cannot be attached "
            f"(supported: {sorted(SUPPORTED_IMAGE_MEDIA_TYPES)})")
    try:
        raw = base64.b64decode(data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise McpContentError(f"{where}: image data is not valid base64") from exc
    if not raw:
        raise McpContentError(f"{where}: image data is empty")
    # mime 已在支持集内校验过（Literal 收窄）；pydantic 构造期还会再验一次
    return ImageAttachmentV1.from_bytes(raw, media_type=cast("ImageMediaType", mime))


def _resource_text(item: dict[str, Any], where: str) -> str:
    """embedded resource：text 资源标注 uri 后内联，blob 资源给显式占位。"""
    resource = item.get("resource")
    if not isinstance(resource, dict):
        raise McpContentError(f"{where}: field 'resource' must be an object")
    uri = _require_str(resource, "uri", f"{where}.resource")
    mime = resource.get("mimeType") or "unknown"
    if isinstance(resource.get("text"), str):
        return f"[resource {uri} ({mime})]\n{resource['text']}"
    if isinstance(resource.get("blob"), str):
        size = _base64_size(resource["blob"])
        return f"[resource {uri}: {mime}, {size} bytes binary, not shown]"
    raise McpContentError(f"{where}.resource: needs a string 'text' or 'blob'")


def _resource_link_text(item: dict[str, Any], where: str) -> str:
    """resource_link：只给引用（本客户端不向模型暴露 resources/read，不代为抓取）。"""
    uri = _require_str(item, "uri", where)
    name = item.get("name") or uri
    mime = item.get("mimeType") or "unknown"
    description = item.get("description")
    suffix = f": {description}" if isinstance(description, str) and description else ""
    return f"[resource link {name} <{uri}> ({mime}){suffix}]"


def _block_to_parts(
    item: dict[str, Any], where: str, *, attach_images: bool,
) -> tuple[str | None, ImageAttachmentV1 | None]:
    """单个内容块 → (文本片段, 图片附件)；二者至多其一非 None。"""
    kind = item.get("type")
    if kind == "text":
        return _require_str(item, "text", where), None
    if kind == "image":
        if attach_images:
            return None, _image_attachment(item, where)
        mime = _require_str(item, "mimeType", where)
        size = _base64_size(_require_str(item, "data", where))
        return f"[image: {mime}, {size} bytes, not attached]", None
    if kind == "audio":
        mime = _require_str(item, "mimeType", where)
        size = _base64_size(_require_str(item, "data", where))
        return f"[audio: {mime}, {size} bytes, not shown: audio content is unsupported]", None
    if kind == "resource":
        return _resource_text(item, where), None
    if kind == "resource_link":
        return _resource_link_text(item, where), None
    # 规范外 / 未来新增类型：给出显式占位而非把整块（可能含二进制）塞进文本
    return f"[unsupported MCP content type {kind!r}]", None


def _structured_already_in_text(structured: dict[str, Any], texts: list[str]) -> bool:
    """文本块里是否已有与 structuredContent JSON 等价的一段（规范建议 server 这样做）。"""
    for text in texts:
        candidate = text.strip()
        if not candidate.startswith("{"):
            continue
        try:
            if json.loads(candidate) == structured:
                return True
        except json.JSONDecodeError:
            continue
    return False


def convert_tool_result(result: dict[str, Any], *, attach_images: bool) -> McpToolOutput:
    """把 MCP ``tools/call`` 的 result 投影为内核工具结果。

    Args:
        result: JSON-RPC result 对象（``content`` / ``structuredContent`` / ``isError``）。
            ``content`` 缺失按空列表处理（仅返回 structuredContent 的 server 常见）。
        attach_images: True → image 块转图片附件；False → 显式占位文本。

    Returns:
        ``McpToolOutput``；文本侧各片段按出现顺序以换行拼接，附件按出现顺序排列。

    Raises:
        McpContentError: ``content`` 非数组、内容块非对象或缺必填字段、图片 MIME 不可作
            附件、base64 非法、``structuredContent`` 非对象。
    """
    content = result.get("content")
    if content is None:
        content = []
    if not isinstance(content, list):
        raise McpContentError("result.content must be an array")
    texts: list[str] = []
    attachments: list[ImageAttachmentV1] = []
    for index, item in enumerate(content):
        where = f"content[{index}]"
        if not isinstance(item, dict):
            raise McpContentError(f"{where} must be an object")
        text, attachment = _block_to_parts(item, where, attach_images=attach_images)
        if text is not None:
            texts.append(text)
        if attachment is not None:
            attachments.append(attachment)
    structured = result.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            raise McpContentError("result.structuredContent must be an object")
        # server 未按规范把结构化结果序列化进 text 块时补上，保证模型能读到
        if not _structured_already_in_text(structured, texts):
            texts.append(json.dumps(structured, ensure_ascii=False))
    return McpToolOutput(
        text="\n".join(texts),
        is_error=bool(result.get("isError")),
        attachments=tuple(attachments),
        structured_content=structured,
    )


__all__ = ["McpContentError", "McpToolOutput", "convert_tool_result"]
