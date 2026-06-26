"""压缩占位符前缀 —— 跨策略共享的幂等守卫标记。

SurgicalTrim / Offload 等策略改写 ``function_call_output`` 的 payload 后,在
output 文本前置可识别前缀,使任何后续 pass / 重复触发都能跳过本类产物,防二次
处理(二次剪枝 / 把 stub 再压成乱码)。

原 SurgicalTrim 内部私有常量上提至此,供多策略共用(见 change
``compaction-offload-strategy`` design D3)。
"""

from __future__ import annotations

# dedup pass 产物:纯重复 tool 结果被替换为 md5 摘要占位
DEDUP_PREFIX = "[duplicate"
# soft-trim / hard-clear pass 产物:中段截断 / 整体清除占位
PRUNED_PREFIX = "[pruned:"
# offload 产物:超大结果落盘后的 stub 指针占位
OFFLOAD_PREFIX = "[offloaded:"

# 所有压缩占位符前缀 —— is_placeholder 的识别集
_ALL_PREFIXES: tuple[str, ...] = (DEDUP_PREFIX, PRUNED_PREFIX, OFFLOAD_PREFIX)


def is_placeholder(text: str) -> bool:
    """text 是否为任一压缩策略的占位符产物 —— 防二次处理。

    Args:
        text: 待判定的 ``function_call_output`` payload 文本。

    Returns:
        命中任一已知占位符前缀返回 True;普通输出 / 空串返回 False。
    """
    return text.startswith(_ALL_PREFIXES)
