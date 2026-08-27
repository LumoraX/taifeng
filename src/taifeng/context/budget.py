"""ContextBudget —— token 预算估算。

参照：codex codex-rs/core/src/compact.rs::estimate_message_tokens

估算策略（粗略，足够触发判断）：
    - 文本：len(text) / 3.5（中英混合的经验比例）
    - 图像：~1500 token（256×256 base）
    - reasoning：实际 reasoning_tokens 字段
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.image_input import ImageInputPolicy, InputCostEstimator


def estimate_text_tokens(text: str) -> int:
    """粗估 text 的 token 数。"""
    return max(1, int(len(text) / 3.5))


def _estimate_image_tokens(
    item: ResponseItem,
    *,
    image_input_policy: ImageInputPolicy | None,
    input_cost_estimator: InputCostEstimator | None,
    model: str,
) -> int:
    """估算 user item 内图片 token；有业务策略时复用完整 admission header。"""
    raw = item.payload.get("attachments", [])
    if not isinstance(raw, list):
        return 0
    images = [value for value in raw if isinstance(value, dict) and value.get("kind") == "image"]
    if not images:
        return 0
    if image_input_policy is None or not image_input_policy.enabled:
        return 1500 * len(images)
    from taifeng.llm.image_input import (
        ConservativeImageCostEstimator,
        ImageAttachmentV1,
        admit_image_attachments,
    )

    attachments = [ImageAttachmentV1.model_validate(value) for value in images]
    inspected = admit_image_attachments(attachments, image_input_policy)
    estimator = input_cost_estimator or ConservativeImageCostEstimator(
        image_input_policy.unknown_model_token_ceiling
    )
    return sum(
        estimator.estimate_image_tokens(
            model=model,
            media_type=image.attachment.media_type,
            width=image.width,
            height=image.height,
            detail=image.attachment.detail,
        )
        for image in inspected
    )


def estimate_item_tokens(
    item: ResponseItem,
    *,
    image_input_policy: ImageInputPolicy | None = None,
    input_cost_estimator: InputCostEstimator | None = None,
    model: str = "",
) -> int:
    """估算单条 ResponseItem 的 token 占用，图片走可注入保守估算器。"""
    payload = item.payload
    if item.kind in ("user_message", "assistant_message", "system_injection"):
        return estimate_text_tokens(str(payload.get("text", ""))) + _estimate_image_tokens(
            item,
            image_input_policy=image_input_policy,
            input_cost_estimator=input_cost_estimator,
            model=model,
        )
    if item.kind == "function_call":
        return estimate_text_tokens(
            str(payload.get("name", "")) + str(payload.get("arguments", ""))
        ) + 10  # 调用结构开销
    if item.kind == "function_call_output":
        return estimate_text_tokens(str(payload.get("output", "")))
    if item.kind == "reasoning":
        return estimate_text_tokens(str(payload.get("text", "")))
    if item.kind == "compacted":
        return estimate_text_tokens(str(payload.get("summary", "")))
    return 50


def estimate_history_tokens(
    items: list[ResponseItem],
    *,
    image_input_policy: ImageInputPolicy | None = None,
    input_cost_estimator: InputCostEstimator | None = None,
    model: str = "",
) -> int:
    """估算完整历史 token，并把统一图片策略传给每条 user item。"""
    return sum(
        estimate_item_tokens(
            item,
            image_input_policy=image_input_policy,
            input_cost_estimator=input_cost_estimator,
            model=model,
        )
        for item in items
    )


def estimate_item_bytes(item: ResponseItem) -> int:
    """估算单条 ResponseItem 序列化后的 UTF-8 字节数（粗略，足够做护栏判断）。"""
    payload = item.payload
    parts: list[str] = []
    for key in ("text", "output", "arguments", "summary", "name"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    size = sum(len(p.encode("utf-8")) for p in parts)
    attachments = payload.get("attachments")
    if isinstance(attachments, list) and attachments:
        size += len(
            json.dumps(
                attachments,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    return size


def estimate_history_bytes(items: list[ResponseItem]) -> int:
    """估算整段 history 序列化后的字节数（G2b body-size 护栏用）。"""
    return sum(estimate_item_bytes(it) for it in items)


@dataclass(frozen=True)
class ContextBudget:
    """token 预算配置。

    Attributes:
        context_window: 模型上下文窗口（如 200k for Claude Sonnet 4）
        soft_limit_ratio: 触发 mid-turn 压缩的比例（默认 0.85）
        hard_limit_ratio: 必须压缩否则报错的比例（默认 0.95）
        preserve_tail_messages: 压缩时保留尾部消息数
        max_request_bytes: 发送前请求体字节数硬上限（G2b）。None=不启用（默认，
            行为不变）；设值后超限在发送前抛 RequestTooLargeError 而非等 provider 4xx
    """

    context_window: int = 200_000
    soft_limit_ratio: float = 0.85
    hard_limit_ratio: float = 0.95
    preserve_tail_messages: int = 4
    max_request_bytes: int | None = None

    @property
    def soft_limit(self) -> int:
        return int(self.context_window * self.soft_limit_ratio)

    @property
    def hard_limit(self) -> int:
        return int(self.context_window * self.hard_limit_ratio)

    def is_soft_exceeded(self, current: int) -> bool:
        return current >= self.soft_limit

    def is_hard_exceeded(self, current: int) -> bool:
        return current >= self.hard_limit
