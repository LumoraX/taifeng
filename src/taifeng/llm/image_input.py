"""图片输入的 canonical admission、格式检查和保守成本估算。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
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

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        media_type: ImageMediaType,
        detail: ImageDetail = "auto",
    ) -> ImageAttachmentV1:
        """从原始字节构造 canonical attachment（自动算 base64 / size / sha256）。

        工具作者返回图片附件的便捷入口——手写 base64 与 digest 容易出错，且错了
        要到 admission 才暴露。这里一次算对，三个字段天然自洽。

        Args:
            data: 图片原始字节（非 base64、非 Data URL）。
            media_type: 图片 MIME；须在 ``SUPPORTED_IMAGE_MEDIA_TYPES`` 内。
            detail: provider 侧细节档位，默认 ``auto``。

        Returns:
            字段自洽、可直接通过 admission 的 ``ImageAttachmentV1``。
        """
        return cls(
            media_type=media_type,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content=base64.b64encode(data).decode("ascii"),
            detail=detail,
        )


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
        """按模型族应用已登记的官方图片 token 规则。"""
        del media_type
        normalized_model = model.rsplit("/", 1)[-1].lower()
        if normalized_model.startswith("gpt-5.6"):
            return _gpt_56_image_tokens(width, height, detail)
        if not model.startswith(("gpt-5", "gpt-4.1", "gpt-4o")):
            return self.unknown_model_token_ceiling
        if detail == "low":
            return 85
        if detail in ("auto", "high", "original"):
            tiles = ((max(1, width) + 511) // 512) * ((max(1, height) + 511) // 512)
            return 85 + 170 * max(1, tiles)
        return self.unknown_model_token_ceiling  # pragma: no cover - Literal 已限制


def _fit_dimensions(width: int, height: int, max_side: int) -> tuple[int, int]:
    """保持宽高比缩入正方形上界，且绝不放大小图。"""
    safe_width = max(1, width)
    safe_height = max(1, height)
    scale = min(1.0, max_side / safe_width, max_side / safe_height)
    return max(1, math.floor(safe_width * scale)), max(
        1, math.floor(safe_height * scale)
    )


def _patch_count(width: int, height: int) -> int:
    """返回覆盖整数像素 canvas 所需的 32×32 patch 数。"""
    return math.ceil(width / 32) * math.ceil(height / 32)


def _fit_patch_budget(width: int, height: int, budget: int) -> tuple[int, int]:
    """按官方 adjusted shrink factor 缩小并保证最终 patch 不超预算。"""
    if _patch_count(width, height) <= budget:
        return width, height
    shrink = math.sqrt((32**2 * budget) / (width * height))
    width_in_patches = width * shrink / 32
    height_in_patches = height * shrink / 32
    adjustment = min(
        math.floor(width_in_patches) / width_in_patches,
        math.floor(height_in_patches) / height_in_patches,
    )
    adjusted = shrink * adjustment
    return max(1, math.floor(width * adjusted)), max(
        1, math.floor(height * adjusted)
    )


def _gpt_56_image_tokens(
    width: int,
    height: int,
    detail: ImageDetail,
) -> int:
    """计算 GPT-5.6 的 32×32 patch 数与 1.2 token multiplier。"""
    if detail == "low":
        resized_width, resized_height = _fit_dimensions(width, height, 512)
    elif detail == "high":
        resized_width, resized_height = _fit_dimensions(width, height, 2048)
        resized_width, resized_height = _fit_patch_budget(
            resized_width,
            resized_height,
            2500,
        )
    else:
        resized_width, resized_height = _fit_dimensions(width, height, 65535)
    return math.ceil(_patch_count(resized_width, resized_height) * 1.2)


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


def _skip_gif_sub_blocks(data: bytes, offset: int) -> int:
    """跳过 GIF extension/image data 的 size-prefixed sub-block 序列。"""
    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        if offset + size > len(data):
            raise InvalidImageError("GIF sub-block is truncated")
        offset += size
    raise InvalidImageError("GIF sub-block terminator is missing")


def _inspect_gif(data: bytes) -> tuple[int, int, int]:
    """按 block 结构解析 GIF logical screen 与 image descriptor。"""
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise InvalidImageError("GIF signature is invalid")
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    offset = 13
    if data[10] & 0x80:
        offset += 3 * (2 ** ((data[10] & 0x07) + 1))
    frames = 0
    while offset < len(data):
        marker = data[offset]
        if marker == 0x3B:
            break
        if marker == 0x21:
            if offset + 2 > len(data):
                raise InvalidImageError("GIF extension is truncated")
            offset = _skip_gif_sub_blocks(data, offset + 2)
            continue
        if marker != 0x2C or offset + 10 > len(data):
            raise InvalidImageError("GIF block structure is invalid")
        packed = data[offset + 9]
        offset += 10
        if packed & 0x80:
            offset += 3 * (2 ** ((packed & 0x07) + 1))
        if offset >= len(data):
            raise InvalidImageError("GIF image data is truncated")
        offset = _skip_gif_sub_blocks(data, offset + 1)
        frames += 1
    if frames == 0:
        raise InvalidImageError("GIF has no image frame")
    return width, height, frames


def _inspect_webp(data: bytes) -> tuple[int, int, int]:
    """解析 VP8X、lossy VP8 与 lossless VP8L 的 canvas 尺寸。"""
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("WebP signature is invalid")
    if int.from_bytes(data[4:8], "little") + 8 > len(data):
        raise InvalidImageError("WebP RIFF body is truncated")
    kind = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    payload = data[20 : 20 + chunk_size]
    if len(payload) != chunk_size:
        raise InvalidImageError("WebP image header is truncated")
    if kind == b"VP8X" and len(payload) >= 10:
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height, 2 if payload[0] & 0x02 else 1
    if kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return width, height, 1
    if kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, 1
    raise InvalidImageError("WebP image header is invalid")


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


def admit_tool_attachments(
    attachments: tuple[ImageAttachmentV1, ...], policy: ImageInputPolicy
) -> list[dict[str, Any]]:
    """工具附件的入口准入 —— 必须在 durable append **之前**执行。

    与 user 消息入口同源复用 ``admit_image_attachments`` 的数量 / 字节 / MIME /
    尺寸 / 帧数校验；``ImageAttachmentV1`` 自身在构造期已保证 base64 canonical、
    size 与 sha256 三者自洽。

    为什么必须前置：渲染期才失败意味着脏 item 已经落进 JSONL，冷恢复重放时会在
    同一处反复炸，且无法靠重试恢复——只能改数据。

    Args:
        attachments: 工具返回的附件元组。空元组直接短路返回（非图片工具零影响，
            此时即便策略未启用也不报错）。
        policy: 业务注入的图片输入策略。

    Returns:
        可直接落 JSONL 的 payload dict 列表，顺序与入参一致。

    Raises:
        UnsupportedModalityError: 策略未启用却返回了附件，或 MIME 不被允许。
        ImageCountExceededError: 附件数超出 ``max_images``。
        AttachmentTooLargeError: 解码字节超出上限。
        InvalidImageError: size / sha256 / 尺寸 / 帧数不合法。
    """
    if not attachments:
        return []
    if not policy.enabled:
        raise UnsupportedModalityError(
            "tool returned image attachments but image input policy is disabled"
        )
    admit_image_attachments(list(attachments), policy)
    return [attachment.model_dump() for attachment in attachments]


def redact_sensitive_request_data(value: object) -> Any:
    """递归脱敏 request JSON 中的图片正文与 provider 加密状态。"""
    if isinstance(value, list):
        return [redact_sensitive_request_data(item) for item in value]
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
    redacted = {
        key: redact_sensitive_request_data(item)
        for key, item in value.items()
        if key != "encrypted_content"
    }
    if "encrypted_content" in value:
        redacted["provider_state_redacted"] = True
    return redacted


def redact_image_bodies(value: object) -> Any:
    """兼容旧调用名，统一执行完整敏感请求脱敏。"""
    return redact_sensitive_request_data(value)
