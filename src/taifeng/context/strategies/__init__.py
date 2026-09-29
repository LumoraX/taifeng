"""压缩策略实现 —— Handoff / Sliding / SurgicalTrim / Offload / MultimodalEviction 五档谱系，
外加把压缩计算挪到后台的包装策略 BackgroundCompaction。

设计文档：docs/architecture/context-compression.md §内置策略
"""

from taifeng.context.strategies.background import BackgroundCompactionStrategy
from taifeng.context.strategies.handoff import HandoffCompactionStrategy
from taifeng.context.strategies.multimodal_evict import MultimodalEvictionStrategy
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy

__all__ = [
    "BackgroundCompactionStrategy",
    "HandoffCompactionStrategy",
    "MultimodalEvictionStrategy",
    "OffloadStrategy",
    "SlidingWindowStrategy",
    "SurgicalTrimStrategy",
]
