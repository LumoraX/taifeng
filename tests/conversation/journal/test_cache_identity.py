"""Audited projection snapshot cache 的完整文件身份回归测试。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.models import user_message
from taifeng.conversation.transcript import JsonlMessageStore
from tests.conversation.journal.projector_test_support import (
    _NOW,
    _conversation_record,
    _create_explicit_projection,
    _encoded,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_same_inode_equal_size_rewrite_with_restored_mtime_invalidates_cache(
    tmp_path: Path,
) -> None:
    """ctime 必须识别 dev/inode/size/mtime 全部不变的等长外部改写。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    item = user_message(text="one", thread_id="thr_explicit").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    projector = JournalConversationProjector(store)
    await projector.project(envelopes, ack)
    path = tmp_path / "thr_explicit.jsonl"
    before_stat = path.stat()
    before_scans = store.projection_scan_count("thr_explicit")
    original = path.read_bytes()
    changed = original.replace(b'"text":"one"', b'"text":"bad"', 1)
    assert changed != original
    assert len(changed) == len(original)

    with path.open("r+b") as stream:
        stream.write(changed)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(
        path,
        ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
    )
    changed_stat = path.stat()
    assert changed_stat.st_dev == before_stat.st_dev
    assert changed_stat.st_ino == before_stat.st_ino
    assert changed_stat.st_size == before_stat.st_size
    assert changed_stat.st_mtime_ns == before_stat.st_mtime_ns
    if changed_stat.st_ctime_ns == before_stat.st_ctime_ns:
        pytest.skip("filesystem does not expose a distinct nanosecond ctime")

    result = await projector.project(envelopes, ack)

    assert result.stale is True
    assert result.failure_class == "item_id_conflict"
    assert store.projection_scan_count("thr_explicit") == before_scans + 1
    await store.close()
