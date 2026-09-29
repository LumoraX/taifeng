"""CompressionStrategy 协议与协调器。

参照：
    - codex codex-rs/core/src/compact.rs
    - claw-code crates/runtime/src/compact.rs
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.context.budget import ContextBudget
    from taifeng.context.engine import ContextEngine
    from taifeng.context.injection import InitialContextInjection
    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.client import ModelClientSession

    ModelSessionFactory = Callable[[str | None], ModelClientSession]

CompressionPhase = Literal["pre_turn", "mid_turn", "manual", "overflow"]

AuditSupport = Literal["fold", "fold_model"]
"""策略声明的审计模式支持（类属性 ``audit_support``；没有该属性 = 不支持）。

- ``fold``：折叠式、不调用模型——结果是「一段 history 被一条 ``compacted`` 条目替代」；
- ``fold_model``：折叠式、调用模型，且只经 ``CompressionContext.model_session`` 调用。

原地改写条目的策略（裁剪、驱逐、落盘）不是折叠式：改写后的条目不进 Journal，
hot history 会与 Journal 不一致，故不能在审计模式下使用。
"""


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

    model_session: ModelSessionFactory | None = None
    """需要调用模型的策略应当从这里取会话（入参是模型名，None = 默认模型）。

    None（默认）= 策略用自己持有的客户端。审计模式下内核在此提供受审计的会话：经它发起的
    每次调用都会落账；绕过它直接用客户端的调用不会落账，也不被允许（ADR 0094）。
    """


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
    detail: dict[str, int] = field(default_factory=dict)
    """策略自报的结构化明细计数（如 surgical_trim 的 deduped / soft_trimmed /
    hard_cleared）。默认空 dict —— 既有策略零改动兼容；turn 组装
    ``compaction_completed`` 事件时透传（R3 机读，不编码进 reason 字符串）。"""
    strategy: str = ""
    """给出这个结果的策略名；由协调器填写，策略自己不必设置。"""


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


def _named(result: CompressionResult, strategy: str) -> CompressionResult:
    """给结果记上策略名；策略自己已经写了的保留。"""
    return result if result.strategy else replace(result, strategy=strategy)


class CompressionOrchestrator:
    """按优先级倒序尝试多策略；第一个返回 trigger 的策略执行。"""

    def __init__(
        self,
        strategies: list[CompressionStrategy],
        *,
        context_engine: ContextEngine | None = None,
    ) -> None:
        """
        Args:
            strategies: 压缩策略，按 priority 倒序尝试。
            context_engine: 上下文引擎（ADR 0093）；None = 每次采样发送完整 history。
                随协调器到达每一个 runner（根 turn、子 skill、分离派发的 child）。
        """
        self._strategies = sorted(strategies, key=lambda s: -s.priority)
        self.context_engine = context_engine

    @property
    def strategies(self) -> tuple[CompressionStrategy, ...]:
        """按优先级倒序的策略（只读视图；如工具结果上限据此判断是否配置了 offload）。"""
        return tuple(self._strategies)

    async def maybe_compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult | None:
        for strat in self._strategies:
            if strat.should_trigger(ctx):
                return _named(await strat.compress(ctx, injection), strat.name)
        return None

    async def force_compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult | None:
        """无视 should_trigger，以最高优先级策略强制压缩。

        为何绕过 should_trigger：各策略按**本地 token 估算**判阈值，而
        overflow 反应式自愈（A1）的成因恰是「本地估算偏低、provider 已判超长」——
        此时 should_trigger 必返回 None，maybe_compress 压不动。本路径直接取最高
        优先级策略执行压缩，覆盖该窗口。无任何策略时返回 None（调用方据此退化硬失败）。

        Args:
            ctx: 压缩上下文。
            injection: 初始上下文注入语义（overflow 自愈走 DO_NOT_INJECT 保 cache anchor）。

        Returns:
            最高优先级策略的压缩结果；无策略时 None。
        """
        if not self._strategies:
            return None
        strat = self._strategies[0]
        return _named(await strat.compress(ctx, injection), strat.name)
