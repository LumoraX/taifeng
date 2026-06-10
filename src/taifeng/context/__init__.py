"""上下文管理 —— ContextBudget、cache-aware 压缩、InitialContextInjection。

设计文档：docs/architecture/context-compression.md
决策记录：docs/decisions/0004-cache-aware-compression.md
"""

from taifeng.context.budget import (
    ContextBudget,
    estimate_history_tokens,
    estimate_item_tokens,
    estimate_text_tokens,
)
from taifeng.context.cache_stats import CacheBreakEvent, CacheBreakReason, PromptCacheStats
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionPhase,
    CompressionResult,
    CompressionStrategy,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.memory import (
    CompositeMemoryStore,
    MemoryStore,
    NullMemoryStore,
)
from taifeng.context.pinned_state import (
    PinnedStateRegistry,
    PinnedStateSource,
)
from taifeng.context.strategies import (
    HandoffCompactionStrategy,
    SlidingWindowStrategy,
    SurgicalTrimStrategy,
)

__all__ = [
    "CacheBreakEvent",
    "CacheBreakReason",
    "CompositeMemoryStore",
    "MemoryStore",
    "NullMemoryStore",
    "CompressionContext",
    "CompressionOrchestrator",
    "CompressionPhase",
    "CompressionResult",
    "CompressionStrategy",
    "CompressionTrigger",
    "ContextBudget",
    "HandoffCompactionStrategy",
    "InitialContextInjection",
    "PinnedStateRegistry",
    "PinnedStateSource",
    "PromptCacheStats",
    "SlidingWindowStrategy",
    "SurgicalTrimStrategy",
    "estimate_history_tokens",
    "estimate_item_tokens",
    "estimate_text_tokens",
]
