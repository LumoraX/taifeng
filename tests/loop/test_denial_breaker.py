"""DenialBreaker —— turn 内连续拒绝断路器单元测试。

覆盖 spec `turn-resource-guards`：consecutive 触发 / 成功重置 / 滑窗触发 /
单次闩锁 / 未配置阈值永不触发 / snapshot 字段。
"""

from __future__ import annotations

from taifeng.loop.denial_breaker import DenialBreaker, DenialBreakerConfig


def test_consecutive_threshold_opens_once() -> None:
    """连续第 3 次 deny 触发；record_denial 仅在恰好触发那次返回 True。"""
    b = DenialBreaker(DenialBreakerConfig(max_consecutive_denials=3))
    assert b.record_denial("shell_exec") is False
    assert b.record_denial("shell_exec") is False
    assert b.record_denial("shell_exec") is True  # 恰好触发
    assert b.opened
    assert b.record_denial("shell_exec") is False  # 已 open，不重复


def test_success_resets_consecutive() -> None:
    """2 deny → 1 success → 2 deny（阈值 3）→ 不触发。"""
    b = DenialBreaker(DenialBreakerConfig(max_consecutive_denials=3))
    b.record_denial("t")
    b.record_denial("t")
    b.record_success()
    b.record_denial("t")
    assert b.record_denial("t") is False
    assert not b.opened


def test_recent_window_threshold() -> None:
    """滑窗：窗口 4 内 deny 达 3（夹杂成功，consecutive 不够）→ 触发。"""
    b = DenialBreaker(DenialBreakerConfig(
        max_consecutive_denials=10, max_recent_denials=3, window_size=4,
    ))
    b.record_denial("a")
    b.record_success()
    b.record_denial("b")
    assert b.record_denial("c") is True  # 窗口 [D,S,D,D] → recent=3
    assert b.opened


def test_window_evicts_old() -> None:
    """旧记录滑出窗口后不再计入 recent。"""
    b = DenialBreaker(DenialBreakerConfig(
        max_consecutive_denials=10, max_recent_denials=3, window_size=3,
    ))
    b.record_denial("a")
    b.record_success()
    b.record_success()
    b.record_denial("b")  # 窗口 [S,S,D] → recent=1
    assert not b.opened


def test_no_thresholds_never_opens() -> None:
    """两阈值都 None → 任意 deny 永不触发（默认关闭语义的内层保险）。"""
    b = DenialBreaker(DenialBreakerConfig())
    for _ in range(100):
        assert b.record_denial("t") is False
    assert not b.opened


def test_snapshot_fields() -> None:
    """snapshot 四字段齐全（事件 data 来源）。"""
    b = DenialBreaker(DenialBreakerConfig(max_consecutive_denials=2, window_size=8))
    b.record_denial("shell_exec")
    b.record_denial("file_write")
    snap = b.snapshot()
    assert snap == {
        "consecutive": 2,
        "recent": 2,
        "window_size": 8,
        "last_denied_target": "file_write",
    }
