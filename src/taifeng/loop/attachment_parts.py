"""附件 payload → provider-neutral parts 的投影，以及 user 消息入队前准入。

conversation 里的附件以 canonical dict 落 JSONL（``ImageAttachmentV1`` /
``FileAttachmentV1`` 的 ``model_dump``）；每轮 prompt 重建时在这里按业务策略复核
（admission）并投影为 ``ImagePart`` / ``FilePart``。legacy 入队路径
（``AgentEngine.submit``）在 durable append 前调用同一实现，保证「入队准入」与
「每轮重建」的能力门与校验规则只有一份，不会漂移。

- user 消息：图片要求 client 声明 ``"image"``、文件要求 ``"file"``；能力不足**抛错**
  而非降级（用户明确塞了附件却看不到 = 输入被吞，必须让调用方知道）。
- 工具结果：只有图片附件（tool-image-attachment 契约），能力不足的降级在
  ``prompt._tool_output_content``，本模块只提供投影。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from taifeng.llm.client import model_capabilities as declared_capabilities
from taifeng.llm.errors import UnsupportedModalityError
from taifeng.llm.file_input import FileAttachmentV1, admit_file_attachments
from taifeng.llm.image_input import ImageAttachmentV1, admit_image_attachments
from taifeng.llm.types import FilePart, ImagePart

if TYPE_CHECKING:
    from collections.abc import Iterator

    from taifeng.llm.client import ModelCapabilities
    from taifeng.llm.file_input import FileInputPolicy
    from taifeng.llm.image_input import ImageInputPolicy


def extract_attachments(
    payload: dict[str, Any], kind: Literal["image", "file"]
) -> list[dict[str, Any]]:
    """从 item payload 取出指定 kind 的附件，非法形状按无附件处理（保持原顺序）。"""
    raw = payload.get("attachments", [])
    if not isinstance(raw, list):
        return []
    return [
        attachment
        for attachment in raw
        if isinstance(attachment, dict) and attachment.get("kind") == kind
    ]


def to_image_parts(
    images: list[dict[str, Any]], policy: ImageInputPolicy
) -> list[ImagePart]:
    """canonical 图片 payload → provider-neutral ImagePart（含 admission）。"""
    attachments = [ImageAttachmentV1.model_validate(image) for image in images]
    return [
        ImagePart(
            media_type=image.attachment.media_type,
            base64_data=image.attachment.content,
            size=image.attachment.size,
            sha256=image.attachment.sha256,
            detail=image.attachment.detail,
        )
        for image in admit_image_attachments(attachments, policy)
    ]


def to_file_parts(files: list[dict[str, Any]], policy: FileInputPolicy) -> list[FilePart]:
    """canonical 文件 payload → provider-neutral FilePart（含 admission）。"""
    attachments = [FileAttachmentV1.model_validate(file) for file in files]
    return [file.attachment.to_part() for file in admit_file_attachments(attachments, policy)]


def user_attachment_parts(
    payload: dict[str, Any],
    *,
    image_input_policy: ImageInputPolicy,
    file_input_policy: FileInputPolicy,
    model_capabilities: ModelCapabilities,
) -> list[ImagePart | FilePart]:
    """user 消息附件 → 按原 attachment 顺序交错的图片 / 文件 parts。

    先过能力门（client 自己的声明），再按类批量 admission（数量 / 累计字节是
    「同类附件」口径），最后按原顺序合并，保证模型看到的附件次序与用户提交一致。

    Raises:
        UnsupportedModalityError: client 未声明对应模态，或业务策略未启用 / MIME 不允许。
        LLMError 子类: admission 的其余拒绝（数量、字节、canonical、结构）。
    """
    raw = payload.get("attachments", [])
    if not isinstance(raw, list):
        return []
    images = extract_attachments(payload, "image")
    files = extract_attachments(payload, "file")
    if images and "image" not in model_capabilities.input_modalities:
        raise UnsupportedModalityError("model client does not support image input")
    if files and "file" not in model_capabilities.input_modalities:
        raise UnsupportedModalityError("model client does not support file input")
    # 空列表不调 admission：未启用的策略面对「无该类附件」不应报错
    image_parts: Iterator[ImagePart] = iter(
        to_image_parts(images, image_input_policy) if images else []
    )
    file_parts: Iterator[FilePart] = iter(
        to_file_parts(files, file_input_policy) if files else []
    )
    ordered: list[ImagePart | FilePart] = []
    for attachment in raw:
        # 与 extract_attachments 同一过滤口径，逐个取回对应类别的下一个 part
        kind = attachment.get("kind") if isinstance(attachment, dict) else None
        if kind == "image":
            ordered.append(next(image_parts))
        elif kind == "file":
            ordered.append(next(file_parts))
    return ordered


def admit_user_attachments(
    attachments: list[dict[str, Any]],
    *,
    image_input_policy: ImageInputPolicy,
    file_input_policy: FileInputPolicy,
    model_client: object,
) -> None:
    """legacy 入队准入：在 enqueue 与 durable append 前复核 user 消息的全部附件。

    与 prompt 重建共用 ``user_attachment_parts``（同一能力门与 admission），不合格
    的附件在这里就抛出，绝不留下每次恢复都会报错的脏历史。

    Args:
        attachments: ``UserMessage.attachments`` 原样列表。
        image_input_policy: 已解析（非 None）的图片策略。
        file_input_policy: 已解析（非 None）的文件策略。
        model_client: 本 engine 的 model client（读取其声明的输入能力）。
    """
    user_attachment_parts(
        {"attachments": attachments},
        image_input_policy=image_input_policy,
        file_input_policy=file_input_policy,
        model_capabilities=declared_capabilities(model_client),
    )


__all__ = [
    "admit_user_attachments",
    "extract_attachments",
    "to_file_parts",
    "to_image_parts",
    "user_attachment_parts",
]
