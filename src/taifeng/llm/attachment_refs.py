"""附件的引用形态 —— 字节在 ``AttachmentStore``，会话里只留引用与已校验元数据（ADR 0148）。

内联形态（``ImageAttachmentV1`` / ``FileAttachmentV1``，``encoding="base64"``）把正文放在条目
payload 里；引用形态（``encoding="store"``）只带一个不透明引用，以及 admission 时从字节读出
的元数据（图片宽高、PDF 页数）。同一条目的 ``attachments`` 列表里两种形态可以并存，
判别字段是 ``encoding``（``kind`` 仍区分图片与文件）。

元数据只在字节通过 admission 时写入，之后被信任：token / 成本估算读它们，不再解码正文。

参照（只学范式，不抄代码）：DeepSeek Harness attachment seam 的 ``ImageAttachmentRef``——
会话事件里只有不透明引用与已校验的尺寸 / 长度。差异：taifeng 的引用另带 PDF 页数，
供按页估算；引用格式由存储实现决定，内核不解析。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from taifeng.llm.types import ImageDetail, ImageMediaType  # noqa: TC001 —— pydantic 运行期需要

# 引用的字符集与长度：它会出现在给模型看的描述里（``path=attachment:<ref>``），
# 不能含空白、路径分隔符或控制字符
ATTACHMENT_REF_PATTERN = r"[A-Za-z0-9._:-]{1,200}"
_REF_RE = re.compile(rf"^{ATTACHMENT_REF_PATTERN}$")
# 描述里指向附件存储的路径前缀（模型据此用 file_read 读回）
ATTACHMENT_PATH_PREFIX = "attachment:"
# 引用形态的 encoding 取值
STORE_ENCODING = "store"


def is_valid_ref(value: object) -> bool:
    """``value`` 是否是一个形状合法的引用（不检查存储里是否存在）。"""
    return isinstance(value, str) and bool(_REF_RE.match(value))


class ImageAttachmentRefV1(BaseModel):
    """图片附件的引用形态：不含正文，带已校验的尺寸。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["image"] = "image"
    encoding: Literal["store"] = "store"
    ref: str = Field(pattern=rf"^{ATTACHMENT_REF_PATTERN}$")
    media_type: ImageMediaType
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    detail: ImageDetail = "auto"


class FileAttachmentRefV1(BaseModel):
    """文件附件的引用形态：不含正文，带已校验的页数（数不出时为 None）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file"] = "file"
    encoding: Literal["store"] = "store"
    ref: str = Field(pattern=rf"^{ATTACHMENT_REF_PATTERN}$")
    media_type: Literal["application/pdf"]
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: int | None = Field(default=None, gt=0)
    filename: str | None = Field(default=None, min_length=1, max_length=255)


def is_ref_attachment(attachment: object) -> bool:
    """payload 里的一个附件 dict 是否是引用形态。"""
    return isinstance(attachment, dict) and attachment.get("encoding") == STORE_ENCODING


def has_ref_attachments(payload: dict[str, Any]) -> bool:
    """payload 的 ``attachments`` 里是否至少有一个引用形态。"""
    raw = payload.get("attachments")
    return isinstance(raw, list) and any(is_ref_attachment(item) for item in raw)


__all__ = [
    "ATTACHMENT_PATH_PREFIX",
    "ATTACHMENT_REF_PATTERN",
    "STORE_ENCODING",
    "FileAttachmentRefV1",
    "ImageAttachmentRefV1",
    "has_ref_attachments",
    "is_ref_attachment",
    "is_valid_ref",
]
