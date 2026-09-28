"""ContextBudget —— token 预算估算。

参照：codex codex-rs/core/src/compact.rs::estimate_message_tokens

估算策略（两层）：
    1. 本地粗估（无实测时的地板）：
       - 文本：len(text) / 3.5（中英混合的经验比例）
       - 图像：~1500 token（256×256 base）或业务估算器
    2. 实测校准（token-accounting-calibration，参照 codex
       ``context_manager/history.rs`` 的「上次真实 usage + 之后新增条目估算」）：
       每次采样成功后用 provider 回报的完整 prompt token 数建一个 ``TokenCalibration``
       锚点；此后估算 = 实测 prompt token + 锚点之后新增条目的本地估算。
       实测天然含 system prompt / 工具 schema / provider 模板开销，本地粗估看不到这些。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
        # 工具返回的图片同样计量：不计等于内核自己的资源账是假的，
        # 且会让 soft/hard limit 在含图会话里形同虚设。
        return estimate_text_tokens(str(payload.get("output", ""))) + _estimate_image_tokens(
            item,
            image_input_policy=image_input_policy,
            input_cost_estimator=input_cost_estimator,
            model=model,
        )
    if item.kind == "reasoning":
        return estimate_text_tokens(str(payload.get("text", "")))
    if item.kind == "compacted":
        return estimate_text_tokens(str(payload.get("summary", "")))
    from taifeng.conversation.models import BOOKKEEPING_ITEM_KINDS

    # 记账类 item（挂起 / spawn 锚 / 战绩 / 工具意图等）不进 LLM 视图，不占上下文
    if item.kind in BOOKKEEPING_ITEM_KINDS:
        return 0
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
class TokenCalibration:
    """上下文 token 实测校准锚点（token-accounting-calibration）。

    采样成功后由 TurnRunner 以 provider 回报的 usage 建立，跨 turn 由 Engine 持有。

    Attributes:
        anchor_len: 该次请求发出时的 history 长度；``-1`` 表示锚点已失效
            （压缩改写了前缀等），此时只剩 ``overhead_tokens`` 可用。
        anchor_item_id: 发出时 history 末项 id（``anchor_len == 0`` 时为 None），
            用于检测前缀是否仍是当初那一段（rewind / 冷重载后会变）。
        prompt_tokens: provider 实测的完整 prompt token 数（含缓存部分，
            口径见 ``extract_usage_anthropic``）。
        overhead_tokens: 实测 - 本地粗估 history 的差（下限 0）——即 system prompt、
            工具 schema、协议模板与粗估误差的合计；锚点失效后作为粗估的加项继续使用。
    """

    anchor_len: int
    anchor_item_id: str | None
    prompt_tokens: int
    overhead_tokens: int

    @property
    def anchor_valid(self) -> bool:
        """锚点是否仍可用于「实测 + 增量」估算。"""
        return self.anchor_len >= 0

    def invalidated(self) -> TokenCalibration:
        """返回锚点失效但保留 overhead 的副本（前缀被压缩改写时调用）。"""
        return replace(self, anchor_len=-1, anchor_item_id=None)


def build_token_calibration(
    sent_history: Sequence[ResponseItem],
    prompt_tokens: int,
    *,
    estimate: Callable[[list[ResponseItem]], int],
) -> TokenCalibration:
    """以一次成功采样的实测 prompt token 数建立校准锚点。

    Args:
        sent_history: 该次请求发出时的 history（发出时刻的前缀，不含本次产出）。
        prompt_tokens: provider 回报的完整 prompt token 数（必须 > 0，调用方保证）。
        estimate: 与估算路径同配置的本地粗估函数（含图片策略）。

    Returns:
        新的 ``TokenCalibration``；``overhead_tokens`` 下限为 0——粗估偏高时不做负修正，
        宁可高估触发压缩，也不低估撞上 provider 的上下文上限。
    """
    items = list(sent_history)
    overhead = max(0, prompt_tokens - estimate(items))
    return TokenCalibration(
        anchor_len=len(items),
        anchor_item_id=items[-1].id if items else None,
        prompt_tokens=prompt_tokens,
        overhead_tokens=overhead,
    )


def calibrated_history_tokens(
    items: Sequence[ResponseItem],
    calibration: TokenCalibration | None,
    *,
    estimate: Callable[[list[ResponseItem]], int],
) -> int:
    """按校准锚点估算当前上下文 token 占用。

    三档，按精度从高到低：
    1. 锚点有效且前缀未变（长度够、末项 id 对得上）→ 实测 prompt + 锚点后新增条目粗估；
    2. 有校准但锚点失效 / 前缀已变 → 全量粗估 + 上次测得的 overhead；
    3. 从未校准 → 全量粗估（旧行为）。

    Args:
        items: 当前 history。
        calibration: 最近一次校准；None = 尚无实测。
        estimate: 本地粗估函数。
    """
    history = list(items)
    if calibration is None:
        return estimate(history)
    n = calibration.anchor_len
    # 前缀校验：长度够且锚点末项 id 一致，才认为锚点之前的内容就是当初实测的那一段
    if calibration.anchor_valid and len(history) >= n and (
        n == 0 or history[n - 1].id == calibration.anchor_item_id
    ):
        return calibration.prompt_tokens + estimate(history[n:])
    return estimate(history) + calibration.overhead_tokens


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
        output_reserve_tokens: 为模型输出预留的 token 数。上下文窗口是输入 + 输出
            共用的，soft / hard 阈值按「窗口 - 预留」计算；0 = 不预留（默认，
            行为不变）。典型取值 = 请求的 max_output_tokens。
        max_tool_result_bytes: 单条工具结果文本进入历史前的 UTF-8 字节上限，超限保头尾、
            省中间并写明省略量；None = 不限。默认 128KiB（约 3–4 万 token）：防止 MCP /
            业务工具的超大输出一次吃掉大半窗口。配置了 OffloadStrategy 时不生效（大结果
            交给 offload 无损落盘）。
    """

    context_window: int = 200_000
    soft_limit_ratio: float = 0.85
    hard_limit_ratio: float = 0.95
    preserve_tail_messages: int = 4
    max_request_bytes: int | None = None
    output_reserve_tokens: int = 0
    max_tool_result_bytes: int | None = 128 * 1024

    def __post_init__(self) -> None:
        """构造期校验：预留必须非负且小于窗口，否则 usable 为 0 / 负，阈值失去意义；
        工具结果上限须留得下截断标记（< 1KiB 的上限截完只剩标记）。"""
        if self.max_tool_result_bytes is not None and self.max_tool_result_bytes < 1024:
            raise ValueError(
                f"max_tool_result_bytes must be >= 1024 or None, got {self.max_tool_result_bytes}")
        if self.output_reserve_tokens < 0:
            raise ValueError(
                f"output_reserve_tokens must be >= 0, got {self.output_reserve_tokens}")
        if self.output_reserve_tokens >= self.context_window:
            raise ValueError(
                "output_reserve_tokens must be < context_window, got "
                f"{self.output_reserve_tokens} >= {self.context_window}")

    @property
    def usable_input_window(self) -> int:
        """可供输入（prompt）使用的窗口 = 窗口 - 输出预留。"""
        return self.context_window - self.output_reserve_tokens

    @property
    def soft_limit(self) -> int:
        return int(self.usable_input_window * self.soft_limit_ratio)

    @property
    def hard_limit(self) -> int:
        return int(self.usable_input_window * self.hard_limit_ratio)

    def is_soft_exceeded(self, current: int) -> bool:
        return current >= self.soft_limit

    def is_hard_exceeded(self, current: int) -> bool:
        return current >= self.hard_limit
