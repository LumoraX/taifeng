"""压缩策略实现 —— HandoffCompactionStrategy / SlidingWindowStrategy。

设计文档：docs/architecture/context-compression.md §内置策略
"""

from taifeng.context.strategies.handoff import HandoffCompactionStrategy
from taifeng.context.strategies.sliding import SlidingWindowStrategy

__all__ = ["HandoffCompactionStrategy", "SlidingWindowStrategy"]
