"""MultimodalEviction 策略 —— 驱逐旧的多模态重载荷（图片 / 文件附件）。

一张图片折合上千 token，一份 PDF 更多；它们在被模型看过之后，对后续推理的价值远低于
它们占用的上下文。本策略把**旧条目**上的附件换成一行描述（类型、大小、内容摘要），
文本原样保留，最近的若干条带附件条目不动。

参照（只学范式，不抄代码）：
    - openclaw context-pruning —— 旧的 image part 被替换为占位文本
    - codex —— 超限时从历史里剥离图片

与 ``surgical_trim`` 的分工：后者剪的是工具**文本**输出，附件只是在文本被剪时顺带丢掉；
用户消息上的附件、文本很短但带着大图的工具结果，它都管不到。本策略只看附件、不动文本，
两者可以同时配置（本策略更便宜，建议优先级更高）。

与上游的差异：
    - **只改写 payload、永不删条目** —— 条目顺序、身份与调用配对不变（R5 / G1b）。
    - 描述里保留内容摘要（sha256 前缀）与文件名：模型仍能指称「那张图 / 那份文档」，
      业务也能据摘要从自己的存储里取回原件。
    - 「最近」按**带附件的条目**计数而非按附件数：一次截图带两张图是一个整体。

全程 LLM-free。R4 取消采用协作式检查点（每处理若干条目 ``await sleep(0)``）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import EVICTED_PREFIX

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem

# 可带附件的条目类型 → 承载文本的 payload 键
_TEXT_KEYS: dict[str, str] = {"user_message": "text", "function_call_output": "output"}
# 协作取消检查点的间隔（条目数）
_YIELD_EVERY = 16


def _attachment_size(attachment: dict[str, Any]) -> int | None:
    """附件声明的字节数；缺失或非法返回 None（不猜）。"""
    size = attachment.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size


def _describe(attachment: dict[str, Any]) -> str:
    """一个附件的一行描述：类型、大小、文件名（如有）、内容摘要前缀。"""
    media_type = attachment.get("media_type")
    parts = [media_type if isinstance(media_type, str) and media_type else "unknown type"]
    size = _attachment_size(attachment)
    parts.append(f"{size / 1024:.1f}KB" if size is not None else "unknown size")
    filename = attachment.get("filename")
    if isinstance(filename, str) and filename:
        parts.append(filename)
    digest = attachment.get("sha256")
    if isinstance(digest, str) and digest:
        parts.append(f"sha256={digest[:8]}")
    return " ".join(parts)


def _stub(evicted: list[dict[str, Any]]) -> str:
    """被驱逐附件的占位文本。"""
    noun = "attachment" if len(evicted) == 1 else "attachments"
    described = "; ".join(_describe(attachment) for attachment in evicted)
    return f"{EVICTED_PREFIX} {len(evicted)} {noun}: {described}]"


class MultimodalEvictionStrategy:
    """旧条目上的图片 / 文件附件换成描述；文本与最近的附件不动。

    Args:
        priority: orchestrator 排序优先级。默认 25：高于 ``surgical_trim``（20，本策略更便宜、
            收益更大），低于 ``offload``（30，无损档优先）。
        trigger_ratio: token 估算占 context_window 的比例 ≥ 此值、且存在可驱逐的附件时触发。
        keep_recent: 保留最近多少条**带附件的条目**不动（按条目计，不按附件数）。
        protect_tail_messages: 尾部保护条数 —— 最后 N 条内的附件永不驱逐，也不占
            ``keep_recent`` 的名额。
        min_attachment_bytes: 只驱逐声明大小 ≥ 此值的附件；大小缺失的附件视为可驱逐。
        allow_head_evict: 仅 pre_turn（BEFORE_LAST_USER_MESSAGE）下允许越过 cache anchor；
            越过时如实标 ``cache_invalidated=True``（R2）。

    Raises:
        ValueError: 参数越界。
    """

    name = "multimodal_evict"

    def __init__(
        self,
        *,
        priority: int = 25,
        trigger_ratio: float = 0.3,
        keep_recent: int = 2,
        protect_tail_messages: int = 4,
        min_attachment_bytes: int = 0,
        allow_head_evict: bool = False,
    ) -> None:
        if not 0.0 < trigger_ratio <= 1.0:
            raise ValueError(f"trigger_ratio must be within (0, 1], got {trigger_ratio!r}")
        for label, value in (
            ("keep_recent", keep_recent),
            ("protect_tail_messages", protect_tail_messages),
            ("min_attachment_bytes", min_attachment_bytes),
        ):
            if value < 0:
                raise ValueError(f"{label} must be non-negative, got {value!r}")
        self.priority = priority
        self._trigger_ratio = trigger_ratio
        self._keep_recent = keep_recent
        self._protect_tail = protect_tail_messages
        self._min_bytes = min_attachment_bytes
        self._allow_head_evict = allow_head_evict

    # ---- 候选 ----

    def _is_heavy(self, attachment: object) -> bool:
        """该附件是否在驱逐范围内。"""
        if not isinstance(attachment, dict):
            return False
        size = _attachment_size(attachment)
        return size is None or size >= self._min_bytes

    def _bearing(self, item: ResponseItem) -> bool:
        """条目是否带着至少一个可驱逐的附件。"""
        if item.kind not in _TEXT_KEYS:
            return False
        attachments = item.payload.get("attachments")
        return isinstance(attachments, list) and any(map(self._is_heavy, attachments))

    def _candidates(self, history: list[ResponseItem], start: int) -> list[int]:
        """窗口 [start, len - protect_tail) 内、排除最近 keep_recent 条之后的可驱逐条目下标。"""
        end = len(history) - self._protect_tail
        bearing = [
            index for index in range(max(start, 0), max(end, 0))
            if self._bearing(history[index])
        ]
        if self._keep_recent == 0:
            return bearing
        return bearing[: -self._keep_recent]

    def _window_start(self, ctx: CompressionContext, injection: object) -> int:
        """可驱逐窗口的起点：常规为 anchor 之后；显式放开且 pre_turn 时从头开始。"""
        if (
            self._allow_head_evict
            and injection == InitialContextInjection.BEFORE_LAST_USER_MESSAGE
        ):
            return 0
        return ctx.cache_anchor_index + 1

    # ---- 触发 ----

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        """有上下文压力、且常规窗口内确有可驱逐的附件才触发。

        没有可驱逐的附件时返回 None：orchestrator 只执行第一个触发的策略，空转会挡住
        后面真正能腾出空间的策略。
        """
        ratio = ctx.token_estimate / max(ctx.budget.context_window, 1)
        if ratio < self._trigger_ratio:
            return None
        start = 0 if self._allow_head_evict and ctx.phase in ("pre_turn", "manual") else (
            ctx.cache_anchor_index + 1
        )
        if not self._candidates(ctx.history, start):
            return None
        return CompressionTrigger(reason="token_limit", threshold_pct=ratio)

    # ---- 改写 ----

    def _evict(self, item: ResponseItem) -> tuple[ResponseItem, list[dict[str, Any]]]:
        """去掉条目上可驱逐的附件，在文本后追加描述；返回 (新条目, 被驱逐的附件)。"""
        attachments = list(item.payload.get("attachments") or [])
        evicted = [a for a in attachments if self._is_heavy(a)]
        kept = [a for a in attachments if not self._is_heavy(a)]
        text_key = _TEXT_KEYS[item.kind]
        payload = dict(item.payload)
        text = str(payload.get(text_key) or "")
        payload[text_key] = f"{text}\n{_stub(evicted)}" if text else _stub(evicted)
        if kept or item.kind == "user_message":
            # user_message 恒带 attachments 键（可为空列表）
            payload["attachments"] = kept
        else:
            # function_call_output 无附件时不写该键（与从未带过附件的条目逐键一致）
            payload.pop("attachments", None)
        return item.model_copy(update={"payload": payload}), evicted

    # ---- 主入口 ----

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        """驱逐候选条目上的附件；就地改写 payload，不增删条目。"""
        history = list(ctx.history)
        candidates = self._candidates(history, self._window_start(ctx, injection))
        detail = {"evicted_items": 0, "evicted_attachments": 0, "evicted_bytes": 0}
        for processed, index in enumerate(candidates, start=1):
            history[index], evicted = self._evict(history[index])
            detail["evicted_items"] += 1
            detail["evicted_attachments"] += len(evicted)
            detail["evicted_bytes"] += sum(_attachment_size(a) or 0 for a in evicted)
            if processed % _YIELD_EVERY == 0:
                await asyncio.sleep(0)  # 协作取消检查点
        await asyncio.sleep(0)  # 协作取消检查点
        if not candidates:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="nothing_to_evict",
                detail=detail,
            )
        crossed = [index for index in candidates if index <= ctx.cache_anchor_index]
        return CompressionResult(
            success=True,
            cache_invalidated=bool(crossed),
            anchor_preserved_until=(
                min(crossed) - 1 if crossed else ctx.cache_anchor_index
            ),
            new_history=history,
            removed_item_count=len(candidates),
            detail=detail,
        )


__all__ = ["MultimodalEvictionStrategy"]
