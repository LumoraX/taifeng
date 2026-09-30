"""文件附件的 durable 形状（session-journal，ADR 0095）。

``AttachmentV1`` 只有图片的形状（没有文件名）。文件附件另立 DTO，不给 ``AttachmentV1`` 加字段：
加字段会改变此后每一条图片附件的 canonical bytes。

独立成模块：``records.py`` 的 ``SubmissionAcceptedV1`` 要引用本 DTO，放进 ``records.py`` 会让它
继续超长；本模块只依赖 ``models``，不成环。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import unicodedata
from typing import Literal

from pydantic import Field, field_validator

from taifeng.conversation.journal.models import JournalModel
from taifeng.llm.types import FileMediaType  # noqa: TC001  # Pydantic 运行期需要


class FileAttachmentRecordV1(JournalModel):
    """完整内联 base64 的文件附件；不接受 URI、Data URL 或临时路径。

    字段与会话层的 ``FileAttachmentV1`` 一一对应：去掉 ``payload_version`` 后即是对话项里的
    附件形状。
    """

    payload_version: Literal[1] = 1
    kind: Literal["file"] = "file"
    media_type: FileMediaType
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding: Literal["base64"] = "base64"
    content: str = Field(min_length=1)
    filename: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("content")
    @classmethod
    def _reject_reference_shape(cls, value: str) -> str:
        """拒绝 Data URL：Journal 只保存裸 canonical base64。"""
        if value.startswith("data:"):
            raise ValueError("file attachment content must not be a Data URL")
        return value

    @field_validator("filename")
    @classmethod
    def _plain_display_name(cls, value: str | None) -> str | None:
        """文件名只是展示名：拒绝路径分隔符与控制字符。"""
        if value is None:
            return value
        if "/" in value or "\\" in value:
            raise ValueError("file attachment filename must not contain path separators")
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("file attachment filename must not contain control characters")
        return value

    def decoded(self) -> bytes:
        """严格解码并校验声明的 size 与小写 SHA-256。"""
        try:
            decoded = base64.b64decode(self.content.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("attachment content is not strict base64") from exc
        if base64.b64encode(decoded).decode("ascii") != self.content:
            raise ValueError("attachment content is not canonical base64")
        if len(decoded) != self.size:
            raise ValueError("attachment decoded size mismatch")
        if hashlib.sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("attachment SHA-256 mismatch")
        return decoded


__all__ = ["FileAttachmentRecordV1"]
