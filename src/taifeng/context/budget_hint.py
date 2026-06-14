"""预算自知提示 —— 上下文用量穿越 soft_limit 时给模型注一条中性预算事实。

ADR 0017 规则②（模型认知回路原语）：让模型知道自己还剩多少上下文预算，从而
**主动收敛**（类比自我 review / 任务清单工作记忆）。本模块只产出**中性事实**
（用了百分之几 / 距 hard limit 还剩多少 token），**不含**「该不该收敛 / 请总结」
之类的产品意见（R1 业务零侵入 + 规则④边界）——具体怎么做交给模型与业务侧。

注入位置与节奏由调用方（TurnRunner）决定：pre-turn 头部一侧、复用既有
``soft_limit`` 阈值、**穿越一次注一次**（回落到 soft 以下复位，再穿越重新通知）。
本模块只提供两个纯函数：``render_budget_hint``（渲染事实文本）与
``evaluate_budget_hint``（穿越判定 + notified 状态流转），便于单测与零状态复用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅类型注解使用（运行时只按属性访问 budget 实例，不引用类名本身）。
    from taifeng.context.budget import ContextBudget


def render_budget_hint(used: int, budget: ContextBudget) -> str:
    """渲染中性预算事实文本（调用方已判定 used 穿越 soft_limit）。

    Args:
        used: 当前历史估算 token 数。
        budget: 预算配置（提供 context_window / hard_limit）。

    Returns:
        形如 ``"Context budget: ~90% of the 1000-token context window is used
        (~50 tokens until the hard limit)."`` 的中性事实串——只陈述事实，不含指令。
    """
    window = budget.context_window
    # 窗口非正属配置异常，但此处只做展示，保守取 0%（不在读路径抛错）。
    pct = round(100 * used / window) if window > 0 else 0
    remaining = max(0, budget.hard_limit - used)
    return (
        f"Context budget: ~{pct}% of the {window}-token context window is used "
        f"(~{remaining} tokens until the hard limit)."
    )


def evaluate_budget_hint(
    used: int, budget: ContextBudget, *, was_notified: bool
) -> tuple[bool, bool]:
    """穿越判定 + notified 状态流转（穿越一次注一次 + 回落复位）。

    Args:
        used: 当前历史估算 token 数。
        budget: 预算配置。
        was_notified: 本「超 soft episode」此前是否已通知过。

    Returns:
        ``(inject, notified)``：
        - 超 soft 且未通知过 → ``(True, True)``：本轮注入一次，置位。
        - 超 soft 且已通知   → ``(False, True)``：不重复注入，保持置位。
        - 未超 soft          → ``(False, False)``：复位（回落后再穿越会重新通知）。
    """
    if budget.is_soft_exceeded(used):
        return (not was_notified, True)
    return (False, False)
