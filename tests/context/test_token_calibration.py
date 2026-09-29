"""token-accounting-calibration 单元测试 —— 实测锚点 + 增量粗估 / 输出预留。

覆盖 ``TokenCalibration`` / ``build_token_calibration`` / ``calibrated_history_tokens``
三档估算与 ``ContextBudget.output_reserve_tokens`` 的阈值与校验，以及按 skill
``max_output_tokens`` 派生生效预留的 ``with_output_reserve``（ADR 0071）。
"""

from __future__ import annotations

import pytest

from taifeng.context.budget import (
    ContextBudget,
    OutputReserveExceedsWindowError,
    TokenCalibration,
    build_token_calibration,
    calibrated_history_tokens,
    estimate_history_tokens,
)
from taifeng.conversation.models import user_message


def _items(n: int, text: str = "x" * 35) -> list:
    """n 条用户消息，每条粗估 10 token（35 字符 / 3.5）。"""
    return [user_message(text, thread_id="t") for _ in range(n)]


def test_calibrated_history_tokens_no_calibration_falls_back_to_estimate() -> None:
    """从未校准 → 全量粗估（旧行为）。"""
    items = _items(3)
    assert calibrated_history_tokens(items, None, estimate=estimate_history_tokens) == 30


def test_calibrated_history_tokens_valid_anchor_uses_measured_plus_tail() -> None:
    """锚点有效 → 实测 prompt + 锚点之后新增条目的粗估。"""
    items = _items(3)
    cal = build_token_calibration(items, 500, estimate=estimate_history_tokens)
    grown = [*items, *_items(2)]
    assert calibrated_history_tokens(grown, cal, estimate=estimate_history_tokens) == 500 + 20


def test_build_token_calibration_records_overhead() -> None:
    """overhead = 实测 - 前缀粗估（system prompt / 工具 schema 等粗估看不到的部分）。"""
    items = _items(3)
    cal = build_token_calibration(items, 500, estimate=estimate_history_tokens)
    assert cal.anchor_len == 3
    assert cal.anchor_item_id == items[-1].id
    assert cal.overhead_tokens == 470


def test_build_token_calibration_overhead_clamped_at_zero() -> None:
    """粗估高于实测时 overhead 取 0：宁可高估触发压缩，也不做负修正。"""
    items = _items(3)
    cal = build_token_calibration(items, 5, estimate=estimate_history_tokens)
    assert cal.overhead_tokens == 0


def test_calibrated_history_tokens_prefix_replaced_uses_estimate_plus_overhead() -> None:
    """前缀被替换（末项 id 对不上）→ 全量粗估 + overhead。"""
    items = _items(3)
    cal = build_token_calibration(items, 500, estimate=estimate_history_tokens)
    replaced = _items(4)
    assert calibrated_history_tokens(
        replaced, cal, estimate=estimate_history_tokens) == 40 + 470


def test_calibrated_history_tokens_history_shorter_than_anchor_uses_overhead() -> None:
    """history 比锚点短（rewind 截断）→ 全量粗估 + overhead。"""
    items = _items(3)
    cal = build_token_calibration(items, 500, estimate=estimate_history_tokens)
    assert calibrated_history_tokens(
        items[:1], cal, estimate=estimate_history_tokens) == 10 + 470


def test_invalidated_calibration_keeps_overhead_only() -> None:
    """invalidated() → 锚点失效，但 overhead 继续修正粗估。"""
    items = _items(3)
    cal = build_token_calibration(items, 500, estimate=estimate_history_tokens).invalidated()
    assert not cal.anchor_valid
    assert calibrated_history_tokens(items, cal, estimate=estimate_history_tokens) == 30 + 470


def test_calibrated_history_tokens_empty_anchor_prefix() -> None:
    """锚点在空 history 上（anchor_len=0）仍有效：实测 + 全部条目粗估。"""
    cal = TokenCalibration(
        anchor_len=0, anchor_item_id=None, prompt_tokens=200, overhead_tokens=200)
    assert calibrated_history_tokens(
        _items(2), cal, estimate=estimate_history_tokens) == 200 + 20


def test_context_budget_output_reserve_shrinks_limits() -> None:
    """输出预留从窗口中扣除后再按比例算 soft / hard。"""
    budget = ContextBudget(
        context_window=10_000, soft_limit_ratio=0.5, hard_limit_ratio=0.9,
        output_reserve_tokens=2_000)
    assert budget.usable_input_window == 8_000
    assert budget.soft_limit == 4_000
    assert budget.hard_limit == 7_200


def test_context_budget_default_reserve_keeps_old_limits() -> None:
    """默认不预留：阈值与引入预留前一致。"""
    budget = ContextBudget(context_window=10_000, soft_limit_ratio=0.5, hard_limit_ratio=0.9)
    assert budget.soft_limit == 5_000
    assert budget.hard_limit == 9_000


@pytest.mark.parametrize("reserve", [-1, 10_000, 20_000])
def test_context_budget_invalid_reserve_raises(reserve: int) -> None:
    """预留为负或不小于窗口 → 构造期报错（阈值会失去意义）。"""
    with pytest.raises(ValueError, match="output_reserve_tokens"):
        ContextBudget(context_window=10_000, output_reserve_tokens=reserve)


@pytest.mark.parametrize("declared", [None, 1_000, 2_000])
def test_with_output_reserve_keeps_budget_when_not_larger(declared: int | None) -> None:
    """未声明或不大于既有预留 → 原样返回同一实例（未声明时一切不变）。"""
    budget = ContextBudget(context_window=10_000, output_reserve_tokens=2_000)
    assert budget.with_output_reserve(declared) is budget


def test_with_output_reserve_raises_reserve_and_limits() -> None:
    """声明更大的输出上限 → 预留取声明值，soft / hard 随之收紧，其余字段不变。"""
    budget = ContextBudget(
        context_window=10_000, soft_limit_ratio=0.5, hard_limit_ratio=0.9,
        preserve_tail_messages=7, max_request_bytes=1234, output_reserve_tokens=1_000)
    eff = budget.with_output_reserve(4_000)
    assert eff.output_reserve_tokens == 4_000
    assert (eff.usable_input_window, eff.soft_limit, eff.hard_limit) == (6_000, 3_000, 5_400)
    assert (eff.preserve_tail_messages, eff.max_request_bytes, eff.context_window) == (
        7, 1234, 10_000)
    assert budget.output_reserve_tokens == 1_000  # 原预算不被改写（frozen 派生）


@pytest.mark.parametrize("declared", [10_000, 20_000])
def test_with_output_reserve_rejects_reserve_not_below_window(declared: int) -> None:
    """声明值不小于窗口 → 显式报错（ValueError 子类），错误里写明来源与数值。"""
    budget = ContextBudget(context_window=10_000)
    with pytest.raises(OutputReserveExceedsWindowError, match="skill 'x' .*10000"):
        budget.with_output_reserve(declared, source="skill 'x' inference.max_output_tokens")
    assert issubclass(OutputReserveExceedsWindowError, ValueError)
