"""图片 canonical admission 与格式检查契约。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from taifeng.context.budget import estimate_item_bytes, estimate_item_tokens
from taifeng.conversation.models import user_message
from taifeng.llm import ImageAttachmentV1 as PublicImageAttachmentV1
from taifeng.llm import ImageInputPolicy as PublicImageInputPolicy
from taifeng.llm.errors import (
    AttachmentTooLargeError,
    ImageCountExceededError,
    InvalidImageError,
    UnsupportedModalityError,
)
from taifeng.llm.image_input import (
    DISABLED_IMAGE_POLICY,
    ConservativeImageCostEstimator,
    ImageAttachmentV1,
    ImageInputPolicy,
    admit_image_attachments,
)


def _png(width: int = 1, height: int = 1) -> bytes:
    """构造只供 header inspector 使用的最小 PNG。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _gif(frame_count: int = 1) -> bytes:
    """构造有指定 image descriptor 数量的最小 GIF header。"""
    descriptor = b",\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    return b"GIF89a\x01\x00\x01\x00\x00\x00\x00" + descriptor * frame_count + b";"


def _attachment(data: bytes, media_type: str = "image/png") -> ImageAttachmentV1:
    """以真实 decoded bytes 生成 canonical attachment。"""
    return ImageAttachmentV1(
        media_type=media_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
    )


POLICY = ImageInputPolicy(
    enabled=True,
    max_images=2,
    max_item_bytes=256,
    max_total_bytes=300,
    allowed_media_types=frozenset({"image/png", "image/gif"}),
    unknown_model_token_ceiling=321,
)


def test_disabled_policy_rejects_before_reading_attachment_body() -> None:
    """默认禁用时，即使正文损坏也只得到稳定 modality 拒绝。"""
    malformed = ImageAttachmentV1(
        media_type="image/png",
        size=1,
        sha256="0" * 64,
        content="not base64",
    )

    with pytest.raises(UnsupportedModalityError):
        admit_image_attachments([malformed], DISABLED_IMAGE_POLICY)


def test_admission_returns_dimensions_and_preserves_attachment_order() -> None:
    """合法图片的维度与输入顺序必须保留。"""
    first = _attachment(_png(width=2, height=3))
    second = _attachment(_gif(), media_type="image/gif")

    inspected = admit_image_attachments([first, second], POLICY)

    assert [(item.width, item.height) for item in inspected] == [(2, 3), (1, 1)]
    assert [item.attachment.sha256 for item in inspected] == [first.sha256, second.sha256]


def test_admission_rejects_noncanonical_base64_before_signature() -> None:
    """padding bits 不 canonical 时不能被宽松 decoder 接受。"""
    attachment = _attachment(_png()).model_copy(update={"content": "Zh=="})

    with pytest.raises(InvalidImageError, match="canonical base64"):
        admit_image_attachments([attachment], POLICY)


def test_admission_rejects_mime_signature_mismatch() -> None:
    """PNG MIME 不能伪装 GIF bytes。"""
    attachment = _attachment(_gif(), media_type="image/png")

    with pytest.raises(InvalidImageError, match="signature"):
        admit_image_attachments([attachment], POLICY)


def test_admission_rejects_animated_gif() -> None:
    """第一阶段仅接受单帧 GIF。"""
    attachment = _attachment(_gif(frame_count=2), media_type="image/gif")

    with pytest.raises(InvalidImageError, match="frame"):
        admit_image_attachments([attachment], POLICY)


def test_admission_applies_count_and_decoded_byte_limits() -> None:
    """数量闸先于正文，单项与总量都按 decoded bytes 计算。"""
    attachment = _attachment(_png())
    too_many = [attachment, attachment, attachment]
    too_large = _attachment(_png() + b"x" * 300)

    with pytest.raises(ImageCountExceededError):
        admit_image_attachments(too_many, POLICY)
    with pytest.raises(AttachmentTooLargeError):
        admit_image_attachments([too_large], POLICY)


def test_unknown_model_image_estimate_uses_policy_ceiling() -> None:
    """未知模型不得把图片成本估算为零。"""
    inspected = admit_image_attachments([_attachment(_png())], POLICY)[0]
    estimator = ConservativeImageCostEstimator(POLICY.unknown_model_token_ceiling)

    assert (
        estimator.estimate_image_tokens(
            model="unregistered",
            media_type=inspected.attachment.media_type,
            width=inspected.width,
            height=inspected.height,
            detail="auto",
        )
        == 321
    )


def test_context_budget_counts_image_tokens_and_body_bytes() -> None:
    """上下文预算必须消费注入的图片估算器，且字节数不能忽略正文。"""
    attachment = _attachment(_png(width=2, height=3))

    class FixedEstimator:
        """记录实际收到的图片 header，并返回稳定估值。"""

        calls: list[tuple[int, int]] = []

        def estimate_image_tokens(self, **kwargs: object) -> int:
            self.calls.append((int(kwargs["width"]), int(kwargs["height"])))
            return 777

    estimator = FixedEstimator()
    item = user_message(
        "inspect",
        thread_id="thread-image-budget",
        attachments=[attachment.model_dump()],
    )

    tokens = estimate_item_tokens(
        item,
        image_input_policy=POLICY,
        input_cost_estimator=estimator,
        model="gpt-5.6",
    )

    assert tokens >= 777
    assert estimator.calls == [(2, 3)]
    assert estimate_item_bytes(item) >= len(attachment.content.encode("ascii"))


def test_image_admission_types_are_available_from_public_llm_api() -> None:
    """业务侧可通过稳定 LLM API 显式配置图片输入。"""
    assert PublicImageAttachmentV1 is ImageAttachmentV1
    assert PublicImageInputPolicy is ImageInputPolicy
