"""SlidingWindowStrategy —— 滑窗兜底压缩。

当 LLM 接力压缩失败 / 不可用时使用。仅保留尾部 N 条 + system 段，
中间内容直接丢弃并写一个 placeholder ``compacted`` 节点。

参照：AutoGen v0.4 ``HeadAndTailChat``
"""

from __future__ import annotations

import logging

from taifeng.context.boundaries import resolve_compaction_range
from taifeng.context.budget import estimate_history_tokens
from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies.handoff import _walk_back_to_safe_boundary
from taifeng.conversation.models import compacted

logger = logging.getLogger(__name__)


class SlidingWindowStrategy:
    """滑窗兜底。"""

    name = "sliding"
    priority = 10

    def __init__(self, *, keep_tail: int = 6) -> None:
        self._keep_tail = keep_tail

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        if ctx.budget.is_hard_exceeded(ctx.token_estimate):
            return CompressionTrigger(
                reason="token_limit",
                threshold_pct=ctx.token_estimate / max(ctx.budget.context_window, 1),
            )
        return None

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        history = ctx.history
        if len(history) <= self._keep_tail + 1:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="too_few_to_compact",
            )

        if injection == InitialContextInjection.DO_NOT_INJECT:
            # anchor=-1（从未压缩/无锚）必须钳到 0：负索引会让 history[:head_end]
            # 变成“保留到倒数第一条”，压缩产物不缩反增（首次 overflow 自愈必死）
            head_end = max(ctx.cache_anchor_index, 0)
        else:
            head_end = 0
            while head_end < len(history) and history[head_end].kind == "system_injection":
                head_end += 1

        tail_start = max(head_end + 1, len(history) - self._keep_tail)
        tail_start = _walk_back_to_safe_boundary(history, tail_start)
        head_end, tail_start = resolve_compaction_range(
            history,
            head_end,
            tail_start,
            protected_before=head_end,
            protected_from=tail_start,
        )

        if tail_start - head_end < 2:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="boundary_too_narrow",
            )

        removed = tail_start - head_end
        thread_id = history[0].thread_id if history else "unknown"
        placeholder = compacted(
            f"[已省略 {removed} 条历史消息（滑窗兜底）。详细内容已不可恢复。]",
            thread_id=thread_id,
            replaced_range=(head_end, tail_start),
            cache_invalidated=injection != InitialContextInjection.DO_NOT_INJECT,
        )
        new_history = history[:head_end] + [placeholder] + history[tail_start:]

        logger.info(
            "sliding compaction: removed=%d, %d → %d tokens",
            removed,
            ctx.token_estimate,
            estimate_history_tokens(new_history),
        )

        return CompressionResult(
            success=True,
            cache_invalidated=injection != InitialContextInjection.DO_NOT_INJECT,
            anchor_preserved_until=head_end - 1
            if injection == InitialContextInjection.DO_NOT_INJECT
            else -1,
            new_history=new_history,
            removed_item_count=removed,
            summary_item_id=placeholder.id,
        )
