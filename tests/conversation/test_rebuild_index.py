"""rebuild_index 工具契约测试。

覆盖 4 个 spec Acceptance：

- 从空 directory 全量重建（删 SQLite 后能从 JSONL 恢复全部 metadata）
- dry_run 不修改 directory
- 损坏首行计入 error_count + 发 rebuild_skipped_corrupt 事件
- 连续调用 idempotent
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from taifeng.conversation import (
    JsonlMessageWriter,
    SqliteThreadDirectory,
    rebuild_index,
)
from taifeng.loop.event import EventMsg


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[EventMsg] = []

    async def handle(self, ev: EventMsg) -> None:
        self.events.append(ev)


def _new_directory(tmp_path: Path) -> SqliteThreadDirectory:
    return SqliteThreadDirectory(
        tmp_path / "taifeng-index.db",
        threads_dir=tmp_path / "threads",
    )


@pytest.mark.asyncio
async def test_rebuild_from_empty_directory_restores_all_metadata(tmp_path: Path) -> None:
    """空 directory + N 个 JSONL → rebuild_index 后 directory 含 N 个 thread。"""
    writer = JsonlMessageWriter(tmp_path / "threads")
    tids = [await writer.create_thread(entry_skill_id="general") for _ in range(5)]

    directory = _new_directory(tmp_path)
    report = await rebuild_index(writer, directory)
    assert report.scanned_count == 5
    assert report.indexed_count == 5
    assert report.error_count == 0
    assert report.elapsed_ms >= 0

    page = await directory.list_threads(limit=10)
    assert {m.thread_id for m in page.items} == set(tids)
    await directory.close()


@pytest.mark.asyncio
async def test_dry_run_does_not_modify_directory(tmp_path: Path) -> None:
    """dry_run=True SHALL 返回正确计数但 directory.upsert_metadata 一次也不被调用。

    用 SpyDirectory 隔离测试 —— SqliteThreadDirectory 自带 schema-rebuild 副作用，
    会干扰对「dry_run 是否真的没写」的纯粹断言。
    """

    class _SpyDirectory:
        """记录所有 upsert 调用的最小 ThreadDirectory 实现。"""

        def __init__(self) -> None:
            self.upserted: list[Any] = []

        async def list_threads(self, **kwargs):  # type: ignore[no-untyped-def]
            from taifeng.conversation import ThreadPage
            return ThreadPage(items=[], next_cursor=None)

        async def get_metadata(self, thread_id):  # type: ignore[no-untyped-def]
            return None

        async def update_metadata(self, thread_id, patch):  # type: ignore[no-untyped-def]
            pass

        async def upsert_metadata(self, meta):  # type: ignore[no-untyped-def]
            self.upserted.append(meta)

    writer = JsonlMessageWriter(tmp_path / "threads")
    for _ in range(3):
        await writer.create_thread(entry_skill_id="general")

    spy = _SpyDirectory()
    report = await rebuild_index(writer, spy, dry_run=True)  # type: ignore[arg-type]
    assert report.scanned_count == 3
    assert report.indexed_count == 3  # dry_run 仍计入「成功解析」
    assert spy.upserted == []  # 关键断言：upsert 一次也没被调用


@pytest.mark.asyncio
async def test_corrupt_first_line_counted_and_emits_event(tmp_path: Path) -> None:
    """某 thread JSONL 首行损坏 SHALL 计入 error_count + 发 rebuild_skipped_corrupt 事件。"""
    writer = JsonlMessageWriter(tmp_path / "threads")
    tid_good = await writer.create_thread(entry_skill_id="general")
    tid_bad = await writer.create_thread(entry_skill_id="general")

    # 人为破坏 tid_bad 的首行
    bad_path = tmp_path / "threads" / f"{tid_bad}.jsonl"
    bad_path.write_text("this is not valid json\n", encoding="utf-8")

    sink = _RecordingSink()
    directory = _new_directory(tmp_path)
    report = await rebuild_index(writer, directory, sink=sink)
    assert report.scanned_count == 2
    assert report.indexed_count == 1
    assert report.error_count == 1

    corrupt_events = [e for e in sink.events if e.msg.kind == "rebuild_skipped_corrupt"]
    assert len(corrupt_events) == 1
    assert str(bad_path) in corrupt_events[0].msg.data["path"]

    # directory 只含好 thread
    page = await directory.list_threads(limit=10)
    assert {m.thread_id for m in page.items} == {tid_good}
    await directory.close()


@pytest.mark.asyncio
async def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    """连续两次 rebuild_index SHALL 终态一致（indexed_count 相等，directory 内容等价）。"""
    writer = JsonlMessageWriter(tmp_path / "threads")
    for _ in range(4):
        await writer.create_thread(entry_skill_id="general")

    directory = _new_directory(tmp_path)
    report1 = await rebuild_index(writer, directory)
    page1 = await directory.list_threads(limit=10)
    ids1 = {m.thread_id for m in page1.items}

    report2 = await rebuild_index(writer, directory)
    page2 = await directory.list_threads(limit=10)
    ids2 = {m.thread_id for m in page2.items}

    assert report2.indexed_count == report1.indexed_count
    assert ids2 == ids1
    await directory.close()


@pytest.mark.asyncio
async def test_rebuild_handles_missing_required_field(tmp_path: Path) -> None:
    """首行是 JSON 但缺 thread_id 等必需字段 SHALL 计入 error_count。"""
    writer = JsonlMessageWriter(tmp_path / "threads")
    tid_ok = await writer.create_thread(entry_skill_id="general")
    # 手动写一个缺字段的伪文件
    bad_path = tmp_path / "threads" / "thr_bad.jsonl"
    bad_path.write_text(
        '{"__meta__": true, "thread_id": "thr_bad"}\n',  # 缺 created_at / updated_at / entry_skill_id
        encoding="utf-8",
    )

    directory = _new_directory(tmp_path)
    report = await rebuild_index(writer, directory)
    assert report.scanned_count == 2
    assert report.indexed_count == 1
    assert report.error_count == 1

    page = await directory.list_threads(limit=10)
    assert {m.thread_id for m in page.items} == {tid_ok}
    await directory.close()
