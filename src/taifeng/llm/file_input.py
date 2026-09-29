"""用户文件输入（首批 PDF）的 canonical admission、结构检查与保守 token 估算。

与 ``image_input`` 同构（契约见 ``docs/architecture/capabilities/llm-file-input.md``）：

- ``FileAttachmentV1`` 是 conversation 的持久化形态，``FilePart`` 是 request 内的
  provider-neutral 形态；两者都只接受完整内联 canonical base64，不接受 Data URL /
  URL / 路径 / file id。
- ``FileInputPolicy`` 由业务显式注入，总闸默认关闭；admission 在 durable append
  之前完成，prompt 重建时再按同一策略复核（defense in depth，防冷恢复读到脏数据）。
- token 估算按页给保守上界：provider 对 PDF 按页计费（逐页文本 + 页面图像），
  页数不可知时取策略的固定上界，绝不按零计。

参照图片输入的实现范式（``image_input.py``），差异：文件没有像素尺寸与帧数，
结构检查改为 PDF 头 / 尾标记，成本按页数而非 patch 估算。
"""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from taifeng.llm.attachment_codec import decode_canonical_base64
from taifeng.llm.errors import (
    AttachmentTooLargeError,
    FileCountExceededError,
    InvalidFileError,
    UnsupportedModalityError,
)
from taifeng.llm.types import FileMediaType, FilePart

SUPPORTED_FILE_MEDIA_TYPES = frozenset({"application/pdf"})

# PDF 头：``%PDF-<major>.<minor>``，规范要求位于文件起始（不接受前置垃圾字节）
_PDF_HEADER = re.compile(rb"^%PDF-\d\.\d")
# PDF 尾标记须出现在最后 1024 字节内（规范允许其后有少量空白 / 换行）
_PDF_EOF_MARKER = b"%%EOF"
_PDF_TRAILER_WINDOW = 1024
# 页对象：``/Type /Page``（排除页树节点 ``/Pages``）。页对象若藏在压缩 object stream
# 里则数不到，此时按「页数未知」走固定上界。增量更新留下的旧页对象会被重复计数，
# 只会让估算偏高——对上界而言是安全方向。
_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![A-Za-z])")


class FileAttachmentV1(BaseModel):
    """可持久化的 canonical inline file attachment V1（首批仅 PDF）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        """拒绝 Data URL 和外置引用，核心层只接受裸 canonical base64。"""
        if value.startswith("data:"):
            raise ValueError("file attachment content must not be a Data URL")
        return value

    @field_validator("filename")
    @classmethod
    def _plain_display_name(cls, value: str | None) -> str | None:
        """文件名只是展示名：拒绝路径分隔符与控制字符，避免被误当路径或注入换行。"""
        if value is None:
            return value
        if "/" in value or "\\" in value:
            raise ValueError("file attachment filename must not contain path separators")
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("file attachment filename must not contain control characters")
        return value

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        media_type: FileMediaType = "application/pdf",
        filename: str | None = None,
    ) -> FileAttachmentV1:
        """从原始字节构造 canonical attachment（自动算 base64 / size / sha256）。

        手写 base64 与 digest 容易出错，且错了要到 admission 才暴露；这里一次算对，
        三个字段天然自洽。

        Args:
            data: 文件原始字节（非 base64、非 Data URL）。
            media_type: 文件 MIME；须在 ``SUPPORTED_FILE_MEDIA_TYPES`` 内。
            filename: 可选展示名（不含路径）。

        Returns:
            字段自洽的 ``FileAttachmentV1``（内容结构仍由 admission 校验）。
        """
        return cls(
            media_type=media_type,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content=base64.b64encode(data).decode("ascii"),
            filename=filename,
        )

    def to_part(self) -> FilePart:
        """投影为 request 内的 provider-neutral ``FilePart``（调用方须先过 admission）。"""
        return FilePart(
            media_type=self.media_type,
            base64_data=self.content,
            size=self.size,
            sha256=self.sha256,
            filename=self.filename,
        )


@dataclass(frozen=True)
class FileInputPolicy:
    """由业务显式注入的文件输入资源、MIME 与成本估算策略。

    Attributes:
        enabled: 总闸；默认关闭，关闭时带文件的 user 消息在 durable append 前被拒。
        max_files: 单条 user 消息允许的文件数上限。
        max_item_bytes: 单个文件 decoded 字节上限。
        max_total_bytes: 单条消息内全部文件 decoded 字节累计上限。
        allowed_media_types: 允许的 MIME 白名单（须是 ``SUPPORTED_FILE_MEDIA_TYPES`` 子集）。
        page_token_ceiling: 每页 token 保守上界（逐页文本 + 页面图像）。
        unknown_file_token_ceiling: 页数不可知时单个文件的 token 固定上界。
    """

    enabled: bool = False
    max_files: int = 1
    max_item_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024
    allowed_media_types: frozenset[str] = SUPPORTED_FILE_MEDIA_TYPES
    page_token_ceiling: int = 5_000
    unknown_file_token_ceiling: int = 32_768

    def __post_init__(self) -> None:
        """在构造期拒绝无法执行的配置。"""
        if self.max_files <= 0:
            raise ValueError("max_files must be positive")
        if self.max_item_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("file byte limits must be positive")
        if self.max_total_bytes < self.max_item_bytes:
            raise ValueError("max_total_bytes must be at least max_item_bytes")
        if self.page_token_ceiling <= 0 or self.unknown_file_token_ceiling <= 0:
            raise ValueError("file token ceilings must be positive")
        if not self.allowed_media_types <= SUPPORTED_FILE_MEDIA_TYPES:
            raise ValueError("allowed_media_types must be supported file MIME types")


DISABLED_FILE_POLICY = FileInputPolicy(enabled=False)


@dataclass(frozen=True)
class InspectedFile:
    """通过 admission 的文件及其安全结构元数据。

    Attributes:
        attachment: 原 canonical attachment。
        page_count: 从 PDF 结构数出的页对象数；数不到（压缩 object stream）时为 None。
    """

    attachment: FileAttachmentV1
    page_count: int | None


def inspect_pdf(data: bytes) -> int | None:
    """校验 PDF 头 / 尾标记并粗数页对象；不做同步文件 I/O、不解析内容流。

    Returns:
        页对象计数；为 0 时返回 None（页对象在压缩流里，页数不可知）。

    Raises:
        InvalidFileError: 缺少 ``%PDF-x.y`` 头或末尾 ``%%EOF`` 标记（截断 / 非 PDF）。
    """
    if not _PDF_HEADER.match(data):
        raise InvalidFileError("PDF header is missing or invalid")
    if _PDF_EOF_MARKER not in data[-_PDF_TRAILER_WINDOW:]:
        raise InvalidFileError("PDF end-of-file marker is missing")
    pages = len(_PDF_PAGE_OBJECT.findall(data))
    return pages or None


def admit_file_attachments(
    attachments: list[FileAttachmentV1], policy: FileInputPolicy
) -> list[InspectedFile]:
    """执行 file policy 与 canonical body 的完整 admission。

    Args:
        attachments: 同一条 user 消息里的全部文件附件（保持原顺序）。
        policy: 业务注入的文件输入策略。

    Returns:
        与入参同序的 ``InspectedFile`` 列表。

    Raises:
        UnsupportedModalityError: 策略未启用，或 MIME 不在白名单。
        FileCountExceededError: 文件数超出 ``max_files``。
        AttachmentTooLargeError: 单个或累计 decoded 字节超限。
        InvalidFileError: base64 非 canonical、size / sha256 不符、PDF 结构非法。
    """
    if not policy.enabled:
        raise UnsupportedModalityError("file input is disabled by policy")
    if len(attachments) > policy.max_files:
        raise FileCountExceededError("file count exceeds policy maximum")
    inspected: list[InspectedFile] = []
    decoded_total = 0
    for attachment in attachments:
        if attachment.media_type not in policy.allowed_media_types:
            raise UnsupportedModalityError("file media type is not allowed by policy")
        data = decode_canonical_base64(
            attachment.content,
            max_bytes=policy.max_item_bytes,
            label="file",
            invalid_error=InvalidFileError,
        )
        if len(data) != attachment.size:
            raise InvalidFileError("file decoded size mismatch")
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            raise InvalidFileError("file SHA-256 mismatch")
        decoded_total += len(data)
        if decoded_total > policy.max_total_bytes:
            raise AttachmentTooLargeError(
                "file decoded total exceeds byte limit",
                estimated_bytes=decoded_total,
                max_bytes=policy.max_total_bytes,
            )
        inspected.append(InspectedFile(attachment=attachment, page_count=inspect_pdf(data)))
    return inspected


def estimate_file_tokens(file: InspectedFile, policy: FileInputPolicy) -> int:
    """单个已准入文件的保守 token 上界：页数 × 每页上界，页数未知取固定上界。"""
    if file.page_count is None:
        return policy.unknown_file_token_ceiling
    return file.page_count * policy.page_token_ceiling


__all__ = [
    "DISABLED_FILE_POLICY",
    "SUPPORTED_FILE_MEDIA_TYPES",
    "FileAttachmentV1",
    "FileInputPolicy",
    "InspectedFile",
    "admit_file_attachments",
    "estimate_file_tokens",
    "inspect_pdf",
]
