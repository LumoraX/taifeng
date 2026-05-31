"""PromptCacheStats —— 预期 vs unexpected break 跟踪。"""

from __future__ import annotations

from taifeng.context.cache_stats import PromptCacheStats


def test_first_turn_no_break() -> None:
    stats = PromptCacheStats()
    ev = stats.record_turn(cache_read=100, cache_creation=50, anchor_expected=False)
    assert ev is None
    assert stats.completion_cache_hits == 1
    assert stats.completion_cache_misses == 0


def test_second_turn_higher_cache_no_break() -> None:
    stats = PromptCacheStats()
    stats.record_turn(cache_read=100, cache_creation=50, anchor_expected=False)
    ev = stats.record_turn(cache_read=150, cache_creation=20, anchor_expected=False)
    assert ev is None
    assert stats.unexpected_cache_breaks == 0


def test_drop_unexpected_break() -> None:
    stats = PromptCacheStats()
    stats.record_turn(cache_read=200, cache_creation=0, anchor_expected=False)
    ev = stats.record_turn(cache_read=50, cache_creation=100, anchor_expected=False)
    assert ev is not None
    assert ev.unexpected is True
    assert ev.token_drop == 150
    assert ev.reason == "unknown_drop"
    assert stats.unexpected_cache_breaks == 1
    assert stats.expected_invalidations == 0


def test_drop_expected_after_compaction() -> None:
    stats = PromptCacheStats()
    stats.record_turn(cache_read=200, cache_creation=0, anchor_expected=False)
    ev = stats.record_turn(
        cache_read=50,
        cache_creation=100,
        anchor_expected=True,
        anchor_expected_reason="compaction_pre_turn",
    )
    assert ev is not None
    assert ev.unexpected is False
    assert ev.reason == "compaction_pre_turn"
    assert stats.unexpected_cache_breaks == 0
    assert stats.expected_invalidations == 1


def test_history_accumulates() -> None:
    stats = PromptCacheStats()
    stats.record_turn(cache_read=100, cache_creation=0, anchor_expected=False)
    stats.record_turn(cache_read=50, cache_creation=0, anchor_expected=False)
    stats.record_turn(cache_read=10, cache_creation=0, anchor_expected=False)
    assert len(stats.history) == 2  # 两次 drop 各一个
