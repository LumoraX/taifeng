"""reconstruct_logical_history —— 把 append-only transcript 顺序重放成逻辑 history。

append-only 主存(R5)在两种情况下与热内存 history 结构性发散:
1. 压缩:被替换的中间 item 不删,placeholder append 到末尾(replaced_range 记区间)。
2. 历史 rewind/rollback:被截断的 item 仍物理留存,marker 记 cut_index。

本函数顺序重放 transcript,复现热内存 history:折叠压缩区间、挪 salvage note、
按 cut_index 截断。对未压缩/未 rewind 的干净 thread 是恒等映射。纯 CPU、无 IO。

设计:docs/superpowers/specs/2026-06-07-cold-rewind-rebuild-design.md §4
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem

# 触发截断的 marker source(rewind / rollback 在内存截断 history,store 仅留 marker)
_TRUNCATING_SOURCES = frozenset({"rewind", "rollback"})
# 压缩 salvage digest 的 source(store 里在 placeholder 前,内存里在 placeholder 后)
_SALVAGE_SOURCE = "memory_pre_evict"


def reconstruct_logical_history(raw: list[ResponseItem]) -> list[ResponseItem]:
    """顺序重放 transcript → 与热内存等价的逻辑 history。

    参数 raw:`MessageStore.load_thread` 按写入序返回的全部 item(保序 + 完整)。
    抛 ValueError:rewind/rollback marker 缺 cut_index(不静默猜下标)。
    """
    logical: list[ResponseItem] = []
    for item in raw:
        if item.kind == "compacted":
            # 折叠被替换区间;若紧邻前一项是 salvage note,挪到 placeholder 之后
            # compacted item 由内部压缩路径构造,replaced_range 必定存在;
            # 缺失说明 store 数据损坏,KeyError 是期望的快速失败(对比 cut_index 用 .get
            # 是为区分「键缺失」与「键在但需校验」两种情形)。
            start, end = item.payload["replaced_range"]
            salvage = None
            if logical and _is_salvage(logical[-1]):
                salvage = logical.pop()
            salvage_tail = [salvage] if salvage is not None else []
            logical = logical[:start] + [item] + salvage_tail + logical[end:]
        elif (
            item.kind == "system_injection"
            and item.payload.get("source") in _TRUNCATING_SOURCES
        ):
            cut = item.payload.get("cut_index")
            if cut is None:
                raise ValueError(
                    f"rewind/rollback marker 缺 cut_index,无法重建逻辑 history:{item.id}"
                )
            logical = logical[:cut]
            # marker 本身不进 logical(热路径只落 store、不进 _history)
        else:
            logical.append(item)
    return logical


def _is_salvage(item: ResponseItem) -> bool:
    """是否压缩 salvage digest(memory_pre_evict note)。"""
    return (
        item.kind == "system_injection"
        and item.payload.get("source") == _SALVAGE_SOURCE
    )


__all__ = ["reconstruct_logical_history"]
