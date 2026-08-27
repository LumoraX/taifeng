"""图片输入的 canonical admission、格式检查和保守成本估算。"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from taifeng.llm.errors import (
    AttachmentTooLargeError,
    ImageCountExceededError,
    InvalidImageError,
    UnsupportedModalityError,
)
from taifeng.llm.types import ImageDetail, ImageMediaType  # noqa: TC001

SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)


class ImageAttachmentV1(BaseModel):
    """可持久化的 canonical inline image attachment V1。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["image"] = "image"
    media_type: ImageMediaType
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding: Literal["base64"] = "base64"
    content: str = Field(min_length=1)
    detail: ImageDetail = "auto"

    @field_validator("content")
    @classmethod
    def _reject_reference_shape(cls, value: str) -> str:
        """拒绝 Data URL 和外置引用，核心层只接受裸 canonical base64。"""
        if value.startswith("data:"):
            raise ValueError("image attachment content must not be a Data URL")
        return value


@dataclass(frozen=True)
class ImageInputPolicy:
    """由业务显式注入的图片输入资源与 MIME 策略。"""

    enabled: bool = False
    max_images: int = 1
    max_item_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024
    allowed_media_types: frozenset[str] = SUPPORTED_IMAGE_MEDIA_TYPES
    unknown_model_token_ceiling: int = 32_768

    def __post_init__(self) -> None:
        """在构造期拒绝无法执行的配置。"""
        if self.max_images <= 0:
            raise ValueError("max_images must be positive")
        if self.max_item_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("image byte limits must be positive")
        if self.max_total_bytes < self.max_item_bytes:
            raise ValueError("max_total_bytes must be at least max_item_bytes")
        if self.unknown_model_token_ceiling <= 0:
            raise ValueError("unknown_model_token_ceiling must be positive")
        if not self.allowed_media_types <= SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ValueError("allowed_media_types must be supported image MIME types")


DISABLED_IMAGE_POLICY = ImageInputPolicy(enabled=False)


@dataclass(frozen=True)
class InspectedImage:
    """通过 admission 后的图片及其安全 header 元数据。"""

    attachment: ImageAttachmentV1
    width: int
    height: int


class InputCostEstimator(Protocol):
    """为图片输入提供保守 token 估值的可注入策略。"""

    def estimate_image_tokens(
        self,
        *,
        model: str,
        media_type: ImageMediaType,
        width: int,
        height: int,
        detail: ImageDetail,
    ) -> int:
        """返回本图片的保守 token 估值。"""
        ...


@dataclass(frozen=True)
class ConservativeImageCostEstimator:
    """未知模型的固定上界估算，绝不把图片成本按零处理。"""

    token_ceiling: int

    def __post_init__(self) -> None:
        """确保调用方不能配置零或负上界。"""
        if self.token_ceiling <= 0:
            raise ValueError("token_ceiling must be positive")

    def estimate_image_tokens(
        self,
        *,
        model: str,
        media_type: ImageMediaType,
        width: int,
        height: int,
        detail: ImageDetail,
    ) -> int:
        """忽略未登记模型细节，返回策略定义的安全上界。"""
        del model, media_type, width, height, detail
        return self.token_ceiling


@dataclass(frozen=True)
class OpenAIImageCostEstimator:
    """OpenAI 图像 detail 的保守 tile 估算；未知模型回退固定上界。"""

    unknown_model_token_ceiling: int = 32_768

    def __post_init__(self) -> None:
        """保证未知模型回退值可用于硬预算。"""
        if self.unknown_model_token_ceiling <= 0:
            raise ValueError("unknown_model_token_ceiling must be positive")

    def estimate_image_tokens(
        self,
        *,
        model: str,
        media_type: ImageMediaType,
        width: int,
        height: int,
        detail: ImageDetail,
    ) -> int:
        """为已登记 OpenAI GPT-5 模型应用保守的 low/high tile 上界。"""
        del media_type
        if not model.startswith(("gpt-5", "gpt-4.1", "gpt-4o")):
            return self.unknown_model_token_ceiling
        if detail == "low":
            return 85
        if detail in ("auto", "high", "original"):
            tiles = ((max(1, width) + 511) // 512) * ((max(1, height) + 511) // 512)
            return 85 + 170 * max(1, tiles)
        return self.unknown_model_token_ceiling  # pragma: no cover - Literal 已限制


def _encoded_limit(decoded_limit: int) -> int:
    """返回最多 decoded_limit bytes 的 canonical base64 最大长度。"""
    return ((decoded_limit + 2) // 3) * 4


def _decode_canonical_base64(content: str, *, max_bytes: int) -> bytes:
    """带 O(1) 编码长度闸门的严格、canonical base64 解码。"""
    if len(content) > _encoded_limit(max_bytes):
        raise AttachmentTooLargeError(
            "image encoded content exceeds byte limit",
            estimated_bytes=len(content),
            max_bytes=_encoded_limit(max_bytes),
        )
    try:
        encoded = content.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise InvalidImageError("image content is not strict base64") from exc
    if base64.b64encode(decoded).decode("ascii") != content:
        raise InvalidImageError("image content is not canonical base64")
    if len(decoded) > max_bytes:
        raise AttachmentTooLargeError(
            "image decoded content exceeds byte limit",
            estimated_bytes=len(decoded),
            max_bytes=max_bytes,
        )
    return decoded


def _inspect_png(data: bytes) -> tuple[int, int, int]:
    """解析 PNG IHDR 的尺寸；第一阶段 PNG 只有静态输入语义。"""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidImageError("PNG signature or IHDR is invalid")
    if data[12:16] != b"IHDR":
        raise InvalidImageError("PNG signature or IHDR is invalid")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), 1


def _inspect_gif(data: bytes) -> tuple[int, int, int]:
    """解析 GIF logical screen 与 image descriptor 数量。"""
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise InvalidImageError("GIF signature is invalid")
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    frames = data[13:].count(b",")
    if frames == 0:
        raise InvalidImageError("GIF has no image frame")
    return width, height, frames


def _inspect_webp(data: bytes) -> tuple[int, int, int]:
    """解析 VP8X canvas 尺寸；其他 WebP header 统一拒绝而非猜测。"""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("WebP signature is invalid")
    if data[12:16] != b"VP8X":
        raise InvalidImageError("WebP requires a VP8X header")
    width = int.from_bytes(data[24:27], "little") + 1
    height = int.from_bytes(data[27:30], "little") + 1
    return width, height, 1


def _inspect_jpeg(data: bytes) -> tuple[int, int, int]:
    """扫描 JPEG SOF segment，拒绝截断或无尺寸 header 的输入。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise InvalidImageError("JPEG signature is invalid")
    offset = 2
    sof_markers = frozenset(
        {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
    )
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise InvalidImageError("JPEG marker sequence is invalid")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9):
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise InvalidImageError("JPEG segment is truncated")
        if marker in sof_markers:
            if length < 7:
                raise InvalidImageError("JPEG SOF segment is invalid")
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height, 1
        offset += length
    raise InvalidImageError("JPEG has no SOF dimensions")


def inspect_image(data: bytes, media_type: ImageMediaType) -> tuple[int, int, int]:
    """按声明 MIME 解析图片 header，并避免做同步文件 I/O。"""
    inspectors = {
        "image/png": _inspect_png,
        "image/jpeg": _inspect_jpeg,
        "image/webp": _inspect_webp,
        "image/gif": _inspect_gif,
    }
    try:
        return inspectors[media_type](data)
    except KeyError as exc:  # pragma: no cover - ImageMediaType 已限制
        raise InvalidImageError("unsupported image media type") from exc


def admit_image_attachments(
    attachments: list[ImageAttachmentV1], policy: ImageInputPolicy
) -> list[InspectedImage]:
    """执行 image policy 与 canonical body 的完整 admission。"""
    if not policy.enabled:
        raise UnsupportedModalityError("image input is disabled by policy")
    if len(attachments) > policy.max_images:
        raise ImageCountExceededError("image count exceeds policy maximum")
    inspected: list[InspectedImage] = []
    decoded_total = 0
    for attachment in attachments:
        if attachment.media_type not in policy.allowed_media_types:
            raise UnsupportedModalityError("image media type is not allowed by policy")
        data = _decode_canonical_base64(attachment.content, max_bytes=policy.max_item_bytes)
        if len(data) != attachment.size:
            raise InvalidImageError("image decoded size mismatch")
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            raise InvalidImageError("image SHA-256 mismatch")
        decoded_total += len(data)
        if decoded_total > policy.max_total_bytes:
            raise AttachmentTooLargeError(
                "image decoded total exceeds byte limit",
                estimated_bytes=decoded_total,
                max_bytes=policy.max_total_bytes,
            )
        width, height, frames = inspect_image(data, attachment.media_type)
        if width <= 0 or height <= 0:
            raise InvalidImageError("image dimensions must be positive")
        if frames != 1:
            raise InvalidImageError("image must contain exactly one frame")
        inspected.append(InspectedImage(attachment=attachment, width=width, height=height))
    return inspected


def redact_image_bodies(value: object) -> Any:
    """递归脱敏 request JSON 中的图片正文，只保留可审核结构描述。"""
    if isinstance(value, list):
        return [redact_image_bodies(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image" and "base64_data" in value:
        return {
            "type": "image",
            "media_type": value.get("media_type"),
            "size": value.get("size"),
            "detail": value.get("detail"),
            "content_redacted": True,
        }
    return {key: redact_image_bodies(item) for key, item in value.items()}
