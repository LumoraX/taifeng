"""压缩策略实现 —— Handoff / Sliding / SurgicalTrim / Offload 四档谱系。

设计文档：docs/architecture/context-compression.md §内置策略
"""

from taifeng.context.strategies.handoff import HandoffCompactionStrategy
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy

__all__ = [
    "HandoffCompactionStrategy",
    "OffloadStrategy",
    "SlidingWindowStrategy",
    "SurgicalTrimStrategy",
]
