"""预算自知提示纯决策逻辑 —— render + evaluate（穿越一次注一次 + 回落复位）。"""
from __future__ import annotations

from taifeng.context.budget import ContextBudget
from taifeng.context.budget_hint import evaluate_budget_hint, render_budget_hint


def _budget() -> ContextBudget:
    # window=1000, soft=850, hard=950（便于精确算百分比/剩余）
    return ContextBudget(context_window=1000, soft_limit_ratio=0.85, hard_limit_ratio=0.95)


def test_render_contains_pct_remaining_and_window():
    """中性事实串应含百分比、剩余至 hard 的 token 数、窗口大小。"""
    text = render_budget_hint(900, _budget())
    assert "90%" in text, f"应含 90%（900/1000），实得：{text}"
    assert "1000" in text, "应含窗口大小 1000"
    assert "50" in text, "应含剩余至 hard(950-900=50) 的 token 数"


def test_render_is_neutral_fact_no_imperative():
    """内容性质 = 中性事实，不含「该收敛 / 请总结」之类的产品指令（R1/规则④边界）。"""
    text = render_budget_hint(900, _budget()).lower()
    for word in ("should", "must", "please", "summarize", "wrap up", "economical"):
        assert word not in text, f"中性事实不应含祈使/产品意见词：{word!r}"


def test_evaluate_under_soft_no_inject_and_resets():
    """未达 soft → 不注入，且 notified 复位（回落后下次穿越能重新通知）。"""
    inject, notified = evaluate_budget_hint(800, _budget(), was_notified=True)
    assert inject is False
    assert notified is False


def test_evaluate_first_crossing_injects():
    """首次穿越 soft（此前未通知）→ 注入一次，notified 置位。"""
    inject, notified = evaluate_budget_hint(860, _budget(), was_notified=False)
    assert inject is True
    assert notified is True


def test_evaluate_staying_over_does_not_reinject():
    """持续超 soft（已通知过）→ 不重复注入，notified 保持。"""
    inject, notified = evaluate_budget_hint(900, _budget(), was_notified=True)
    assert inject is False
    assert notified is True


def test_evaluate_recross_after_drop_reinjects():
    """回落到 soft 以下复位后，再次穿越 → 重新注入（穿越一次注一次的「次」按 episode 计）。"""
    # 回落
    _, after_drop = evaluate_budget_hint(700, _budget(), was_notified=True)
    assert after_drop is False
    # 再穿越
    inject, notified = evaluate_budget_hint(880, _budget(), was_notified=after_drop)
    assert inject is True
    assert notified is True
