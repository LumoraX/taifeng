"""reconstruct_logical_history —— 把 append-only transcript 顺序重放成逻辑 history。

append-only 主存(R5)在两种情况下与热内存 history 结构性发散:
1. 压缩:被替换的中间 item 不删,placeholder append 到末尾(replaced_range 记区间)。
2. 历史 rewind/rollback:被截断的 item 仍物理留存,marker 记 cut_index;并行批次里的
   retry_tool 另记 drop_index(保留范围内被去掉的那条旧结果,ADR 0079);回到某次
   压缩之前另记 undo_compaction(被撤销的压缩条目 id,ADR 0081)。

本函数顺序重放 transcript,复现热内存 history:折叠压缩区间、挪 salvage note、
按 cut_index 截断、按 drop_index 去掉旧结果。对未压缩/未 rewind 的干净 thread 是恒等
映射。纯 CPU、无 IO。

设计:ADR 0016(冷场景重建,决策一)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem

# 触发截断的 marker source(rewind / rollback 在内存截断 history,store 仅留 marker)
_TRUNCATING_SOURCES = frozenset({"rewind", "rollback"})
# 压缩 salvage digest 的 source(store 里在 placeholder 前,内存里在 placeholder 后)
_SALVAGE_SOURCE = "memory_pre_evict"


# 压缩动作自己写下的旁路项:抢救摘要与钉回项(落在 placeholder 之前,不属于「压缩之前」)
_PINNED_SOURCE_PREFIX = "pinned:"


def reconstruct_logical_history(raw: list[ResponseItem]) -> list[ResponseItem]:
    """顺序重放 transcript → 与热内存等价的逻辑 history。

    参数 raw:`MessageStore.load_thread` 按写入序返回的全部 item(保序 + 完整)。
    抛 ValueError:rewind/rollback marker 缺 cut_index,drop_index 越界 / 指向的不是
    function_call_output,或 undo_compaction 指向未知压缩 / 与 cut_index 不符
    (不静默猜下标)。
    """
    return _replay(raw, stop_before_id=None)


def reconstruct_before_compaction(
    raw: list[ResponseItem], compaction_id: str,
) -> list[ResponseItem]:
    """重放到某次压缩**之前**的逻辑 history(压缩作为回访节点,ADR 0081)。

    结果不含该压缩的 placeholder,也不含压缩动作自己写下的旁路项(紧邻 placeholder
    之前的抢救摘要与钉回项)。该压缩之前的压缩、rewind、rollback 照常生效。

    抛 ValueError:raw 里没有这条压缩。
    """
    for item in raw:
        if item.kind == "compacted" and item.id == compaction_id:
            return _strip_compaction_side_items(_replay(raw, stop_before_id=compaction_id))
    raise ValueError(f"compaction not found in transcript:{compaction_id}")


def _replay(raw: list[ResponseItem], *, stop_before_id: str | None) -> list[ResponseItem]:
    """顺序重放;``stop_before_id`` 非 None 时停在该条目之前。"""
    logical: list[ResponseItem] = []
    # 每次压缩之前的逻辑 history(供「撤销某次压缩」的 marker 还原)
    before_compaction: dict[str, list[ResponseItem]] = {}
    for item in raw:
        if stop_before_id is not None and item.id == stop_before_id:
            break
        if item.kind == "compacted":
            before_compaction[item.id] = _strip_compaction_side_items(logical)
            logical = _fold_compacted(logical, item)
        elif (
            item.kind == "system_injection"
            and item.payload.get("source") in _TRUNCATING_SOURCES
        ):
            logical = _apply_truncation(logical, item, before_compaction)
            # marker 本身不进 logical(热路径只落 store、不进 _history)
        else:
            logical.append(item)
    return logical


def _fold_compacted(logical: list[ResponseItem], item: ResponseItem) -> list[ResponseItem]:
    """折叠被替换区间;若紧邻前一项是 salvage note,挪到 placeholder 之后。

    compacted item 由内部压缩路径构造,replaced_range 必定存在;缺失说明 store 数据损坏,
    KeyError 是期望的快速失败(对比 cut_index 用 .get 是为区分「键缺失」与「键在但需校验」)。
    """
    start, end = item.payload["replaced_range"]
    salvage = None
    if logical and _is_salvage(logical[-1]):
        salvage = logical.pop()
    salvage_tail = [salvage] if salvage is not None else []
    return logical[:start] + [item] + salvage_tail + logical[end:]


def _apply_truncation(
    logical: list[ResponseItem],
    marker: ResponseItem,
    before_compaction: dict[str, list[ResponseItem]],
) -> list[ResponseItem]:
    """按 rewind / rollback marker 的坐标截断(或还原到某次压缩之前)。"""
    cut = marker.payload.get("cut_index")
    if cut is None:
        raise ValueError(
            f"rewind/rollback marker 缺 cut_index,无法重建逻辑 history:{marker.id}"
        )
    undo = marker.payload.get("undo_compaction")
    if undo is not None:
        restored = before_compaction.get(undo)
        if restored is None:
            raise ValueError(f"rewind marker undo_compaction 指向未知压缩:{marker.id}")
        if len(restored) != cut:
            raise ValueError(
                f"rewind marker cut_index 与压缩之前的 history 长度不符:{marker.id}"
            )
        return list(restored)
    truncated = logical[:cut]
    _drop_replaced_output(truncated, marker)
    return truncated


def _strip_compaction_side_items(logical: list[ResponseItem]) -> list[ResponseItem]:
    """去掉末尾由压缩动作写下的旁路项(抢救摘要 / 钉回项),返回新列表。"""
    end = len(logical)
    while end > 0 and _is_compaction_side_item(logical[end - 1]):
        end -= 1
    return list(logical[:end])


def _is_compaction_side_item(item: ResponseItem) -> bool:
    """是否压缩动作自己写下的旁路项。"""
    if item.kind != "system_injection":
        return False
    source = item.payload.get("source")
    return source == _SALVAGE_SOURCE or (
        isinstance(source, str) and source.startswith(_PINNED_SOURCE_PREFIX)
    )


def _drop_replaced_output(logical: list[ResponseItem], marker: ResponseItem) -> None:
    """并行批次 retry_tool:去掉保留范围内被重跑调用的旧结果(marker 无 drop_index 则不动)。"""
    drop = marker.payload.get("drop_index")
    if drop is None:
        return
    if type(drop) is not int or not 0 <= drop < len(logical):
        raise ValueError(f"rewind marker drop_index 越界,无法重建逻辑 history:{marker.id}")
    if logical[drop].kind != "function_call_output":
        raise ValueError(
            f"rewind marker drop_index 指向的不是 function_call_output:{marker.id}"
        )
    del logical[drop]


def _is_salvage(item: ResponseItem) -> bool:
    """是否压缩 salvage digest(memory_pre_evict note)。"""
    return (
        item.kind == "system_injection"
        and item.payload.get("source") == _SALVAGE_SOURCE
    )


__all__ = ["reconstruct_before_compaction", "reconstruct_logical_history"]
