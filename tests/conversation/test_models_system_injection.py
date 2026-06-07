"""system_injection 的 additive extra payload(供 rewind/rollback marker 落 cut_index)。"""
from __future__ import annotations

from taifeng.conversation.models import system_injection


def test_system_injection_without_extra_unchanged():
    """不传 extra 时 payload 仅 text + source(向后兼容)。"""
    item = system_injection("hi", thread_id="t1", source="rewind")
    assert item.payload == {"text": "hi", "source": "rewind"}


def test_system_injection_merges_extra():
    """传 extra 时合并进 payload,不覆盖 text/source。"""
    item = system_injection("hi", thread_id="t1", source="rewind", extra={"cut_index": 7})
    assert item.payload == {"text": "hi", "source": "rewind", "cut_index": 7}
