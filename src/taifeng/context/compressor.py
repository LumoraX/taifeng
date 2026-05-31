"""CompressionStrategy 协议与协调器。

参照：
    - codex codex-rs/core/src/compact.rs
    - claw-code crates/runtime/src/compact.rs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from taifeng.context.budget import ContextBudget
from taifeng.context.injection import InitialContextInjection
from taifeng.conversation.models import ResponseItem

CompressionPhase = Literal["pre_turn", "mid_turn", "manual"]


@dataclass(frozen=True)
class CompressionContext:
    """单次压缩决策的输入。"""

    history: list[ResponseItem]
    token_estimate: int
    budget: ContextBudget
    cache_anchor_index: int
    """此索引（含）之前的 history 已被 cache，mid-turn 压缩不应触碰。"""

    phase: CompressionPhase
    available_injections: frozenset[InitialContextInjection]


@dataclass(frozen=True)
class CompressionTrigger:
    reason: Literal["token_limit", "user_request", "tool_overflow", "scheduled"]
    threshold_pct: float


@dataclass(frozen=True)
class CompressionResult:
    success: bool
    cache_invalidated: bool
    """是否破坏了 prompt cache（mid-turn 应为 False）。"""
    anchor_preserved_until: int
    """被保留的 cache anchor 索引（含）。"""
    new_history: list[ResponseItem] = field(default_factory=list)
    """压缩后的完整 history（含保留段 + summary 节点）。"""
    removed_item_count: int = 0
    summary_item_id: str | None = None
    reason: str | None = None
    """失败原因（成功时为 None）。"""
    quality_warnings: tuple[str, ...] = ()
    """非致命的摘要质量警告（如缺失必备分段）；成功也可能带警告，供 telemetry。"""


@runtime_checkable
class CompressionStrategy(Protocol):
    """压缩策略协议。"""

    name: str
    priority: int

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        ...

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        ...


class CompressionOrchestrator:
    """按优先级倒序尝试多策略；第一个返回 trigger 的策略执行。"""

    def __init__(self, strategies: list[CompressionStrategy]) -> None:
        self._strategies = sorted(strategies, key=lambda s: -s.priority)

    async def maybe_compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult | None:
        for strat in self._strategies:
            if strat.should_trigger(ctx):
                return await strat.compress(ctx, injection)
        return None
