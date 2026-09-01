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
    OpenAIImageCostEstimator,
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
    descriptor = b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01\x00\x00"
    return b"GIF89a\x01\x00\x01\x00\x00\x00\x00" + descriptor * frame_count + b";"


def _webp_chunk(fourcc: bytes, payload: bytes) -> bytes:
    """构造单 chunk RIFF WebP，按偶数字节补齐 payload。"""
    padding = b"\x00" if len(payload) % 2 else b""
    body = b"WEBP" + fourcc + len(payload).to_bytes(4, "little") + payload + padding
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _vp8_webp(width: int, height: int) -> bytes:
    """构造只含 lossy VP8 frame header 的 WebP。"""
    payload = (
        b"\x00\x00\x00\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
    )
    return _webp_chunk(b"VP8 ", payload)


def _vp8l_webp(width: int, height: int) -> bytes:
    """构造只含 lossless VP8L dimension bits 的 WebP。"""
    bits = (width - 1) | ((height - 1) << 14)
    return _webp_chunk(b"VP8L", b"\x2f" + bits.to_bytes(4, "little"))


def _vp8x_webp(width: int, height: int, *, animated: bool) -> bytes:
    """构造 VP8X canvas；animation flag 用于 admission 拒绝测试。"""
    flags = 0x02 if animated else 0
    payload = (
        bytes([flags, 0, 0, 0])
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return _webp_chunk(b"VP8X", payload)


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


def test_static_gif_comment_comma_is_not_a_second_frame() -> None:
    """comment extension 中的逗号字节不能冒充 image descriptor。"""
    header = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
    data = _gif().replace(header, header + b"!\xfe\x01,\x00")

    inspected = admit_image_attachments(
        [_attachment(data, media_type="image/gif")],
        POLICY,
    )

    assert [(image.width, image.height) for image in inspected] == [(1, 1)]


@pytest.mark.parametrize(
    ("data", "dimensions"),
    [(_vp8_webp(13, 17), (13, 17)), (_vp8l_webp(19, 23), (19, 23))],
)
def test_webp_vp8_and_vp8l_dimensions_are_supported(
    data: bytes,
    dimensions: tuple[int, int],
) -> None:
    """常见 lossy/lossless WebP 不得因缺 VP8X 扩展头被拒绝。"""
    policy = POLICY.__class__(
        enabled=True,
        max_images=1,
        max_item_bytes=256,
        max_total_bytes=256,
        allowed_media_types=frozenset({"image/webp"}),
    )

    inspected = admit_image_attachments(
        [_attachment(data, media_type="image/webp")],
        policy,
    )

    assert (inspected[0].width, inspected[0].height) == dimensions


def test_animated_vp8x_is_rejected() -> None:
    """VP8X animation flag 必须进入单帧 admission 拒绝路径。"""
    data = _vp8x_webp(11, 7, animated=True)
    policy = ImageInputPolicy(
        enabled=True,
        max_images=1,
        max_item_bytes=256,
        max_total_bytes=256,
        allowed_media_types=frozenset({"image/webp"}),
    )

    with pytest.raises(InvalidImageError, match="frame"):
        admit_image_attachments(
            [_attachment(data, media_type="image/webp")],
            policy,
        )


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


def test_gpt_56_1024_square_high_costs_1229_tokens() -> None:
    """GPT-5.6 high 使用 32×32 patch 与 1.2 multiplier。"""
    estimator = OpenAIImageCostEstimator()

    assert estimator.estimate_image_tokens(
        model="gpt-5.6-sol",
        media_type="image/png",
        width=1024,
        height=1024,
        detail="high",
    ) == 1229


def test_gpt_56_low_does_not_enlarge_small_image() -> None:
    """官方 low 只缩入 512 方框，不把较小图片放大。"""
    estimator = OpenAIImageCostEstimator()

    assert estimator.estimate_image_tokens(
        model="gpt-5.6-terra",
        media_type="image/png",
        width=1,
        height=1,
        detail="low",
    ) == 2


def test_gpt_56_high_applies_adjusted_patch_budget_resize() -> None:
    """超预算图片按官方 adjusted shrink factor 计算真实 patch 覆盖。"""
    estimator = OpenAIImageCostEstimator()

    assert estimator.estimate_image_tokens(
        model="gpt-5.6-luna",
        media_type="image/png",
        width=2048,
        height=1536,
        detail="high",
    ) == 2942


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


def test_from_bytes_computes_canonical_fields() -> None:
    """from_bytes 应自动算出 base64、size 与 sha256，且字段自洽通过构造期校验。"""
    raw = _png()

    attachment = ImageAttachmentV1.from_bytes(raw, media_type="image/png", detail="high")

    assert attachment.kind == "image"
    assert attachment.media_type == "image/png"
    assert attachment.size == len(raw)
    assert attachment.sha256 == hashlib.sha256(raw).hexdigest()
    assert base64.b64decode(attachment.content) == raw
    assert attachment.detail == "high"


def test_from_bytes_defaults_detail_to_auto() -> None:
    """不指定 detail 时沿用契约默认值 auto。"""
    assert ImageAttachmentV1.from_bytes(_png(), media_type="image/png").detail == "auto"


def test_from_bytes_output_passes_admission() -> None:
    """from_bytes 的产物必须能直接通过 admission —— 这正是它存在的意义。"""
    attachment = ImageAttachmentV1.from_bytes(_png(2, 3), media_type="image/png")

    inspected = admit_image_attachments(
        [attachment], ImageInputPolicy(enabled=True, max_images=1)
    )

    assert len(inspected) == 1
    assert (inspected[0].width, inspected[0].height) == (2, 3)


def test_admit_tool_attachments_empty_is_noop_under_disabled_policy() -> None:
    """无附件时即便策略关闭也不报错 —— 非图片工具零影响。"""
    from taifeng.llm.image_input import admit_tool_attachments

    assert admit_tool_attachments((), DISABLED_IMAGE_POLICY) == []


def test_admit_tool_attachments_rejects_when_policy_disabled() -> None:
    """策略未启用却返回附件 → 如实拒绝，不静默丢图。"""
    from taifeng.llm.image_input import admit_tool_attachments

    attachment = ImageAttachmentV1.from_bytes(_png(), media_type="image/png")

    with pytest.raises(UnsupportedModalityError):
        admit_tool_attachments((attachment,), DISABLED_IMAGE_POLICY)


def test_admit_tool_attachments_enforces_count_limit() -> None:
    """超出 max_images 如实抛，不截断。"""
    from taifeng.llm.image_input import admit_tool_attachments

    attachment = ImageAttachmentV1.from_bytes(_png(), media_type="image/png")
    policy = ImageInputPolicy(enabled=True, max_images=1)

    with pytest.raises(ImageCountExceededError):
        admit_tool_attachments((attachment, attachment), policy)


def test_admit_tool_attachments_enforces_byte_limit() -> None:
    """超出单项字节上限如实抛。"""
    from taifeng.llm.image_input import admit_tool_attachments

    attachment = ImageAttachmentV1.from_bytes(_png(), media_type="image/png")
    policy = ImageInputPolicy(
        enabled=True, max_images=2, max_item_bytes=1, max_total_bytes=1
    )

    with pytest.raises(AttachmentTooLargeError):
        admit_tool_attachments((attachment,), policy)


def test_admit_tool_attachments_returns_persistable_payloads_in_order() -> None:
    """通过后返回可直接落 JSONL 的 dict，带 kind 判别键，顺序与入参一致。"""
    from taifeng.llm.image_input import admit_tool_attachments

    first = ImageAttachmentV1.from_bytes(_png(1, 1), media_type="image/png")
    second = ImageAttachmentV1.from_bytes(_png(2, 2), media_type="image/png")
    policy = ImageInputPolicy(enabled=True, max_images=2)

    payloads = admit_tool_attachments((first, second), policy)

    assert [p["kind"] for p in payloads] == ["image", "image"]
    assert [p["sha256"] for p in payloads] == [first.sha256, second.sha256]
