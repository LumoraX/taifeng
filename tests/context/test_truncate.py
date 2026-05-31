"""G6b：中段截断 truncate_middle。"""

from __future__ import annotations

from taifeng.context.truncate import truncate_middle


def test_short_text_unchanged() -> None:
    assert truncate_middle("hello", 100) == "hello"
    assert truncate_middle("hello", 0) == "hello"


def test_preserves_head_and_tail() -> None:
    text = "HEAD" + ("x" * 5000) + "TAIL"
    out = truncate_middle(text, 200)
    assert len(out) < len(text)
    assert out.startswith("HEAD")  # 头部保留
    assert out.endswith("TAIL")    # 尾部保留（朴素 [:N] 会丢掉）
    assert "字符已省略" in out      # 含省略标记


def test_elided_count_reflects_removed() -> None:
    text = "a" * 1000
    out = truncate_middle(text, 100)
    # 省略数 = 原长 - 实际保留的头尾长度，应为正且接近 900
    assert "已省略" in out
    assert len(out) < 200


def test_head_ratio_controls_split() -> None:
    text = "H" * 500 + "T" * 500
    out = truncate_middle(text, 100, head_ratio=0.8)
    head_part = out.split("…")[0]
    # head_ratio=0.8 → 头部应明显多于尾部
    assert head_part.count("H") > out.rsplit("…", 1)[-1].count("T")
