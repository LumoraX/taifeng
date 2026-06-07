"""reconstruct_logical_history —— transcript 重放成逻辑 history。"""
from __future__ import annotations

import pytest

from taifeng.conversation.models import (
    assistant_message,
    compacted,
    system_injection,
    user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history

T = "thr"


def _u(n): return user_message(f"u{n}", thread_id=T)
def _a(n): return assistant_message(f"a{n}", thread_id=T, model="m")


def test_clean_thread_is_identity():
    """无压缩/无 rewind marker → 原样返回。"""
    raw = [_u(1), _a(1), _u(2), _a(2)]
    assert reconstruct_logical_history(raw) == raw


def test_single_compaction_folds_replaced_range():
    """[head, mid, tail, PH@end] → [head, PH, tail]。"""
    head, mid, tail = _u(1), _a(1), _u(2)
    # 压缩发生时内存 history = [head, mid, tail](len 3),replaced_range=(1,2) 替换 mid
    ph = compacted("s", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)
    raw = [head, mid, tail, ph]
    assert reconstruct_logical_history(raw) == [head, ph, tail]


def test_compaction_with_salvage_note_placed_after_placeholder():
    """store 尾 [..., note, PH] → 逻辑 [head, PH, note, tail](note 挪到 PH 后)。"""
    head, mid, tail = _u(1), _a(1), _u(2)
    note = system_injection("digest", thread_id=T, source="memory_pre_evict")
    ph = compacted("s", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)
    raw = [head, mid, tail, note, ph]  # 写序:note 在 PH 前
    assert reconstruct_logical_history(raw) == [head, ph, note, tail]


def test_rewind_marker_truncates_to_cut_index():
    """[保留, 废弃尾, rewind_marker(cut_index=1), re-run] → [保留[:1], re-run]。"""
    keep, dead = _u(1), _a(1)
    marker = system_injection("[rewind]", thread_id=T, source="rewind", extra={"cut_index": 1})
    rerun = _a(2)
    raw = [keep, dead, marker, rerun]
    assert reconstruct_logical_history(raw) == [keep, rerun]


def test_rollback_marker_truncates_to_cut_index():
    keep = _u(1)
    dead = _a(1)
    marker = system_injection("[rollback]", thread_id=T, source="rollback", extra={"cut_index": 1})
    raw = [keep, dead, marker]
    assert reconstruct_logical_history(raw) == [keep]


def test_missing_cut_index_raises():
    """rewind/rollback marker 缺 cut_index → 显式报错,不静默猜。"""
    marker = system_injection("[rewind]", thread_id=T, source="rewind")  # 无 extra
    with pytest.raises(ValueError, match="cut_index"):
        reconstruct_logical_history([_u(1), _a(1), marker])


def test_nested_compaction_folds_in_order():
    """两次压缩顺序折叠:第二个 PH 的 range 针对已折叠的 history。"""
    head = _u(1)
    a1, a2 = _a(1), _a(2)
    ph1 = compacted("s1", thread_id=T, replaced_range=(1, 2), cache_invalidated=True)  # 替换 a1
    # 第一次压缩后内存 = [head, ph1, a2](len 3);第二次替换 a2(index 2)
    ph2 = compacted("s2", thread_id=T, replaced_range=(2, 3), cache_invalidated=True)
    raw = [head, a1, a2, ph1, ph2]
    # 重放:[head,a1,a2] → 遇 ph1 → [head,ph1,a2]
    # → 遇 ph2(range 2,3 针对 [head,ph1,a2]) → [head,ph1,ph2]
    assert reconstruct_logical_history(raw) == [head, ph1, ph2]
