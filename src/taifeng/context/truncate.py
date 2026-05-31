"""中段截断（middle-out）—— 保头尾、省中间，附省略计数。

参照 codex utils/output-truncation（``truncate_middle_with_token_budget``）。
工具大输出 / 压缩输入的最有诊断价值的部分往往在**头**（命令、起始）和**尾**
（错误、结论），中间是冗余。朴素的 ``text[:N]`` 会丢掉尾部错误信息。
"""

from __future__ import annotations

_DEFAULT_MARKER = "\n…[{elided} 字符已省略]…\n"


def truncate_middle(
    text: str,
    max_chars: int,
    *,
    head_ratio: float = 0.6,
    marker: str = _DEFAULT_MARKER,
) -> str:
    """把 ``text`` 截断到约 ``max_chars`` 字符，保留头部 + 尾部，中间用 marker 替换。

    Args:
        text: 原文。
        max_chars: 目标上限（含 marker 的粗略预算；marker 数字位宽带来的微小
            偏差可接受）。``<=0`` 或原文不超限 → 原样返回。
        head_ratio: 预算中分给头部的比例（默认 0.6；尾部得 0.4）。
        marker: 中间省略标记，必须含 ``{elided}`` 占位（填省略字符数）。

    Returns:
        截断后的字符串；若无需截断则原样返回。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    budget = max(0, max_chars - len(marker.format(elided=0)))
    head_len = int(budget * head_ratio)
    tail_len = budget - head_len
    elided = len(text) - head_len - tail_len
    if elided <= 0:
        return text
    head = text[:head_len]
    tail = text[len(text) - tail_len:] if tail_len > 0 else ""
    return head + marker.format(elided=elided) + tail
