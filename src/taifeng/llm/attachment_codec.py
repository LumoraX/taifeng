"""内联附件共享的 canonical base64 解码（图片 / 文件 admission 同源）。

图片与文件附件都只接受「完整内联 canonical base64」：严格字母表、标准 padding、
重新编码后逐字一致，且解码前先用 O(1) 的编码长度闸门挡住超大正文（不为超限输入
分配内存）。两类附件的差别只在报错类型与报错文案里的标签，故抽成一处实现。
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from taifeng.llm.errors import AttachmentTooLargeError

if TYPE_CHECKING:
    from taifeng.llm.errors import InvalidRequestError


def encoded_limit(decoded_limit: int) -> int:
    """返回最多 ``decoded_limit`` 字节正文的 canonical base64 最大长度。"""
    return ((decoded_limit + 2) // 3) * 4


def decode_canonical_base64(
    content: str,
    *,
    max_bytes: int,
    label: str,
    invalid_error: type[InvalidRequestError],
) -> bytes:
    """带 O(1) 编码长度闸门的严格、canonical base64 解码。

    Args:
        content: 附件的 base64 正文（不得带 Data URL 前缀）。
        max_bytes: 单个附件允许的最大 decoded 字节数。
        label: 报错文案里的附件类别（如 ``image`` / ``file``）。
        invalid_error: 编码非法时抛出的错误类型（``InvalidImageError`` 等）。

    Returns:
        解码后的原始字节。

    Raises:
        AttachmentTooLargeError: 编码长度或解码字节超过上限。
        invalid_error: 非严格 base64，或不是 canonical 编码。
    """
    # 编码长度闸门：先于解码，避免为超大输入分配内存
    if len(content) > encoded_limit(max_bytes):
        raise AttachmentTooLargeError(
            f"{label} encoded content exceeds byte limit",
            estimated_bytes=len(content),
            max_bytes=encoded_limit(max_bytes),
        )
    try:
        encoded = content.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise invalid_error(f"{label} content is not strict base64") from exc
    # canonical 校验：非规范 padding / 尾部 bit 会让同一正文有多种编码，破坏 sha256 身份
    if base64.b64encode(decoded).decode("ascii") != content:
        raise invalid_error(f"{label} content is not canonical base64")
    if len(decoded) > max_bytes:
        raise AttachmentTooLargeError(
            f"{label} decoded content exceeds byte limit",
            estimated_bytes=len(decoded),
            max_bytes=max_bytes,
        )
    return decoded
