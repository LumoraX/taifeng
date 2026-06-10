"""IterationBudget 值对象 —— consume/refund/remaining/child 语义测试。

覆盖 spec `turn-resource-guards`：行为等价（cap 语义同裸计数器）、refund clamp、
子预算独立派生（父子总和可超父 cap，对标 hermes 有意语义）、显式子 cap。
"""

from __future__ import annotations

from taifeng.loop.iteration_budget import IterationBudget


def test_consume_until_cap_exhausted() -> None:
    """cap=3：前 3 次 consume True，第 4 次 False；spent/remaining 对账。"""
    b = IterationBudget(cap=3)
    assert [b.consume() for _ in range(3)] == [True, True, True]
    assert b.spent == 3 and b.remaining == 0
    assert b.consume() is False
    assert b.spent == 3  # 失败的 consume 不计数


def test_refund_returns_steps() -> None:
    """refund 退还步数：spent 减、remaining 增。"""
    b = IterationBudget(cap=5)
    for _ in range(3):
        b.consume()
    b.refund(1)
    assert b.spent == 2 and b.remaining == 3


def test_refund_clamps_to_zero() -> None:
    """refund 超过已消费数 → clamp 到 spent==0，不为负。"""
    b = IterationBudget(cap=5)
    b.consume()
    b.refund(10)
    assert b.spent == 0 and b.remaining == 5


def test_child_independent_of_parent() -> None:
    """子预算独立实例：子消费不回写父；父子总和可超父 cap（有意语义）。"""
    parent = IterationBudget(cap=10)
    for _ in range(8):
        parent.consume()
    child = parent.child()
    # 默认 cap = 父**初始** cap（非剩余）
    assert child.remaining == 10
    for _ in range(10):
        assert child.consume() is True
    assert child.consume() is False
    assert parent.spent == 8 and parent.remaining == 2  # 父不受影响


def test_child_explicit_cap() -> None:
    """显式子上限：cap=3 第 4 次 consume False。"""
    parent = IterationBudget(cap=10)
    child = parent.child(cap=3)
    assert [child.consume() for _ in range(4)] == [True, True, True, False]
