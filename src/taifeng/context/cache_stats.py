"""PromptCacheStats —— cache 命中 / 失效跟踪。

参照：claw-code crates/api/src/prompt_cache.rs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CacheBreakReason = Literal[
    "compaction_pre_turn",
    "compaction_manual",
    "compaction_mid_turn_anchor_lost",
    "skill_snapshot_changed",
    "tool_spec_changed",
    "system_prompt_changed",
    "unknown_drop",
]


@dataclass
class CacheBreakEvent:
    """单次 cache break 事件。"""

    unexpected: bool
    reason: CacheBreakReason
    previous_cache_read_input_tokens: int
    current_cache_read_input_tokens: int
    token_drop: int


@dataclass
class PromptCacheStats:
    """累积 cache 统计。Engine 持有一份。"""

    completion_cache_hits: int = 0
    completion_cache_misses: int = 0
    expected_invalidations: int = 0
    unexpected_cache_breaks: int = 0
    total_cache_creation_input_tokens: int = 0
    total_cache_read_input_tokens: int = 0
    last_cache_read_input_tokens: int | None = None
    last_break: CacheBreakEvent | None = None
    history: list[CacheBreakEvent] = field(default_factory=list)

    def record_turn(
        self,
        *,
        cache_read: int,
        cache_creation: int,
        anchor_expected: bool,
        anchor_expected_reason: CacheBreakReason | None = None,
    ) -> CacheBreakEvent | None:
        """记录一次 turn 的 cache 数据，返回 break 事件（如果有）。

        Args:
            cache_read: 本 turn 的 cache_read_input_tokens
            cache_creation: 本 turn 的 cache_creation_input_tokens
            anchor_expected: 业务层是否声明本 turn 会失效 cache（如压缩动 head）
            anchor_expected_reason: 若 anchor_expected=True，给出原因
        """
        self.total_cache_read_input_tokens += cache_read
        self.total_cache_creation_input_tokens += cache_creation
        if cache_read > 0:
            self.completion_cache_hits += 1
        else:
            self.completion_cache_misses += 1

        prev = self.last_cache_read_input_tokens
        break_event: CacheBreakEvent | None = None
        if prev is not None and cache_read < prev:
            # 出现下降 —— 可能是 break
            unexpected = not anchor_expected
            reason: CacheBreakReason = anchor_expected_reason or "unknown_drop"
            break_event = CacheBreakEvent(
                unexpected=unexpected,
                reason=reason,
                previous_cache_read_input_tokens=prev,
                current_cache_read_input_tokens=cache_read,
                token_drop=prev - cache_read,
            )
            if unexpected:
                self.unexpected_cache_breaks += 1
            else:
                self.expected_invalidations += 1
            self.last_break = break_event
            self.history.append(break_event)
        self.last_cache_read_input_tokens = cache_read
        return break_event
