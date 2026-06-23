"""压缩占位符共享模块测试 —— 跨策略幂等守卫前缀识别。

覆盖:dedup / pruned / offload 三类前缀的识别,以及非占位符不误判。
"""

from __future__ import annotations

from taifeng.context.placeholders import (
    DEDUP_PREFIX,
    OFFLOAD_PREFIX,
    PRUNED_PREFIX,
    is_placeholder,
)


def test_dedup_prefix_recognized() -> None:
    """dedup 产物文本应被识别为占位符。"""
    assert is_placeholder(f"{DEDUP_PREFIX} tool output: md5=abc, 3 occurrences]")


def test_pruned_prefix_recognized() -> None:
    """pruned 产物文本应被识别为占位符。"""
    assert is_placeholder(f"{PRUNED_PREFIX} tool output cleared, original 9000 chars]")


def test_offload_prefix_recognized() -> None:
    """offload stub 文本应被识别为占位符(防被其他策略二次处理)。"""
    assert is_placeholder(f"{OFFLOAD_PREFIX} call_id=c1 saved to /x/y]")


def test_plain_text_not_placeholder() -> None:
    """普通 tool 输出不应被误判为占位符。"""
    assert not is_placeholder('{"result": "ok", "rows": 42}')


def test_empty_text_not_placeholder() -> None:
    """空字符串不是占位符。"""
    assert not is_placeholder("")


def test_three_prefixes_distinct() -> None:
    """三类前缀互不相同(避免常量笔误重复)。"""
    assert len({DEDUP_PREFIX, PRUNED_PREFIX, OFFLOAD_PREFIX}) == 3
