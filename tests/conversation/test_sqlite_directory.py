"""SqliteThreadDirectory（ThreadDirectory 协议默认实现）契约测试。

覆盖 8 个 Acceptance 用例：

- 自动建库 + 建表 + WAL
- list_threads 按 updated_at 倒序
- cursor 分页（100 thread / limit=10 / 翻完）
- filter entry_skill + tag 组合 AND 语义
- update_metadata 部分合并保留其它字段
- 损坏 cursor 重置 + directory_cursor_reset 事件
- schema 版本不匹配触发 drop + rebuild + sqlite_schema_rebuilt 事件
- orphan 跳过 + thread_indexed_orphan 事件
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from taifeng.conversation import (
    JsonlMessageWriter,
    SqliteThreadDirectory,
    ThreadDirectory,
    ThreadFilter,
    ThreadMetadata,
    ThreadNotFoundError,
)
from taifeng.loop.event import EventMsg


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[EventMsg] = []

    async def handle(self, ev: EventMsg) -> None:
        self.events.append(ev)


def _new_directory(tmp_path: Path, *, sink: _RecordingSink | None = None, schema_version: int = 1) -> SqliteThreadDirectory:
    """构造空的 SqliteThreadDirectory + 空 threads_dir。"""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(exist_ok=True)
    db_path = tmp_path / "taifeng-index.db"
    return SqliteThreadDirectory(db_path, threads_dir=threads_dir, schema_version=schema_version, sink=sink)


def _make_meta(thread_id: str, *, updated_at: float, entry: str = "general", tags: tuple[str, ...] = (), source: str = "user") -> ThreadMetadata:
    return ThreadMetadata(
        thread_id=thread_id,
        created_at=updated_at,
        updated_at=updated_at,
        entry_skill_id=entry,
        source=source,
        tags=tags,
        extra={},
    )


async def _seed_with_jsonl(directory: SqliteThreadDirectory, writer: JsonlMessageWriter, *, count: int, base_ts: float = 1_700_000_000.0) -> list[ThreadMetadata]:
    """通过 JsonlMessageWriter 创建 N 个 thread 文件 + upsert 元数据到 directory。
    返回创建的 metadata 列表（按 created_at 升序，所以最后一个 updated_at 最大）。
    """
    metas: list[ThreadMetadata] = []
    for i in range(count):
        tid = await writer.create_thread(entry_skill_id="general")
        meta = ThreadMetadata(
            thread_id=tid,
            created_at=base_ts + i,
            updated_at=base_ts + i,
            entry_skill_id="general",
            source="user",
            tags=(),
            extra={},
        )
        await directory.upsert_metadata(meta)
        metas.append(meta)
    return metas


def test_sqlite_directory_satisfies_protocol(tmp_path: Path) -> None:
    """SqliteThreadDirectory SHALL 通过 isinstance(ThreadDirectory) 校验。"""
    directory = _new_directory(tmp_path)
    assert isinstance(directory, ThreadDirectory)


@pytest.mark.asyncio
async def test_auto_create_db_and_tables(tmp_path: Path) -> None:
    """构造 + 首次方法调用 SHALL 自动建 db 文件、建表、开启 WAL。"""
    directory = _new_directory(tmp_path)
    # 触发 lazy init
    await directory.list_threads()
    db_path = tmp_path / "taifeng-index.db"
    assert db_path.exists()

    # 校验表 + WAL 已开
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
        assert "schema_meta" in tables
        assert "thread" in tables
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
    await directory.close()


@pytest.mark.asyncio
async def test_list_threads_order_desc_by_updated_at(tmp_path: Path) -> None:
    """list_threads SHALL 按 updated_at 倒序返回。"""
    directory = _new_directory(tmp_path)
    writer = JsonlMessageWriter(tmp_path / "threads")
    metas = await _seed_with_jsonl(directory, writer, count=3, base_ts=1000.0)

    page = await directory.list_threads(limit=10)
    returned_ids = [m.thread_id for m in page.items]
    expected_ids = [m.thread_id for m in reversed(metas)]  # updated_at 倒序
    assert returned_ids == expected_ids
    assert page.next_cursor is None
    await directory.close()


@pytest.mark.asyncio
async def test_pagination_with_cursor(tmp_path: Path) -> None:
    """100 thread / limit=10 / 翻 10 页 SHALL 拿到全部且无重复无遗漏。"""
    directory = _new_directory(tmp_path)
    writer = JsonlMessageWriter(tmp_path / "threads")
    metas = await _seed_with_jsonl(directory, writer, count=100, base_ts=1000.0)
    expected_ids = [m.thread_id for m in reversed(metas)]

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(15):  # 上限 15 次防死循环
        page = await directory.list_threads(limit=10, cursor=cursor)
        collected.extend(m.thread_id for m in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert collected == expected_ids
    await directory.close()


@pytest.mark.asyncio
async def test_filter_entry_skill_and_tag_and_semantics(tmp_path: Path) -> None:
    """filter (entry_skill + tag) SHALL 同时满足才返回（AND 语义）。"""
    directory = _new_directory(tmp_path)
    writer = JsonlMessageWriter(tmp_path / "threads")

    # 4 个 thread，分别覆盖四种组合
    tid_match = await writer.create_thread(entry_skill_id="metabolic")
    tid_wrong_tag = await writer.create_thread(entry_skill_id="metabolic")
    tid_wrong_skill = await writer.create_thread(entry_skill_id="general")
    tid_neither = await writer.create_thread(entry_skill_id="general")
    await directory.upsert_metadata(_make_meta(tid_match, updated_at=2000.0, entry="metabolic", tags=("prod",)))
    await directory.upsert_metadata(_make_meta(tid_wrong_tag, updated_at=2001.0, entry="metabolic", tags=("dev",)))
    await directory.upsert_metadata(_make_meta(tid_wrong_skill, updated_at=2002.0, entry="general", tags=("prod",)))
    await directory.upsert_metadata(_make_meta(tid_neither, updated_at=2003.0, entry="general", tags=("dev",)))

    page = await directory.list_threads(filter=ThreadFilter(entry_skill_id="metabolic", tag="prod"))
    returned = {m.thread_id for m in page.items}
    assert returned == {tid_match}
    await directory.close()


@pytest.mark.asyncio
async def test_update_metadata_partial_merge_preserves_other_fields(tmp_path: Path) -> None:
    """update_metadata({"extra": {...}}) SHALL 只换 extra，其它字段不变；updated_at 自动刷新。"""
    directory = _new_directory(tmp_path)
    writer = JsonlMessageWriter(tmp_path / "threads")
    tid = await writer.create_thread(entry_skill_id="general")
    original = _make_meta(tid, updated_at=1000.0, entry="general", tags=("a",))
    await directory.upsert_metadata(original)

    before = await directory.get_metadata(tid)
    assert before is not None
    time.sleep(0.01)  # 确保 updated_at 严格变大
    await directory.update_metadata(tid, {"extra": {"cwd": "/work"}})

    after = await directory.get_metadata(tid)
    assert after is not None
    assert after.thread_id == original.thread_id
    assert after.entry_skill_id == original.entry_skill_id
    assert after.tags == original.tags  # 未提及字段保留
    assert after.extra == {"cwd": "/work"}
    assert after.updated_at > before.updated_at
    assert after.created_at == original.created_at  # created_at 不变
    await directory.close()


@pytest.mark.asyncio
async def test_update_metadata_nonexistent_raises_thread_not_found(tmp_path: Path) -> None:
    """update_metadata 不存在的 thread_id SHALL raise ThreadNotFoundError。"""
    directory = _new_directory(tmp_path)
    with pytest.raises(ThreadNotFoundError):
        await directory.update_metadata("not-exist", {"extra": {}})
    await directory.close()


@pytest.mark.asyncio
async def test_cursor_invalid_resets_and_emits_event(tmp_path: Path) -> None:
    """损坏 cursor SHALL 从头返回 + 发 directory_cursor_reset 事件。"""
    sink = _RecordingSink()
    directory = _new_directory(tmp_path, sink=sink)
    writer = JsonlMessageWriter(tmp_path / "threads")
    await _seed_with_jsonl(directory, writer, count=3)

    page = await directory.list_threads(limit=10, cursor="not-a-valid-cursor!!!")
    # 从头返回 3 条
    assert len(page.items) == 3

    reset_events = [e for e in sink.events if e.msg.kind == "directory_cursor_reset"]
    assert len(reset_events) == 1
    assert reset_events[0].msg.data["cursor"] == "not-a-valid-cursor!!!"
    await directory.close()


@pytest.mark.asyncio
async def test_schema_version_mismatch_triggers_rebuild_with_event(tmp_path: Path) -> None:
    """旧 schema_version=1 db 升级到 schema_version=2 启动 SHALL 触发 drop+rebuild
    且发 sqlite_schema_rebuilt 事件（rebuilt_thread_count == JSONL 文件数）。"""
    # Step 1: 用 v1 创建 directory，写 3 个 thread
    directory_v1 = _new_directory(tmp_path, schema_version=1)
    writer = JsonlMessageWriter(tmp_path / "threads")
    metas = await _seed_with_jsonl(directory_v1, writer, count=3)
    await directory_v1.close()

    # Step 2: 用 v2 重新打开同一个 db；应触发 rebuild
    sink = _RecordingSink()
    threads_dir = tmp_path / "threads"
    db_path = tmp_path / "taifeng-index.db"
    directory_v2 = SqliteThreadDirectory(db_path, threads_dir=threads_dir, schema_version=2, sink=sink)
    await directory_v2.list_threads()  # 触发 lazy init

    rebuild_events = [e for e in sink.events if e.msg.kind == "sqlite_schema_rebuilt"]
    assert len(rebuild_events) == 1
    ev = rebuild_events[0]
    assert ev.msg.data["old_version"] == 1
    assert ev.msg.data["new_version"] == 2
    assert ev.msg.data["rebuilt_thread_count"] == 3
    assert ev.msg.data["elapsed_ms"] >= 0

    # rebuild 后所有 thread 仍可被查到
    page = await directory_v2.list_threads(limit=10)
    assert {m.thread_id for m in page.items} == {m.thread_id for m in metas}
    await directory_v2.close()


@pytest.mark.asyncio
async def test_orphan_thread_skipped_with_event(tmp_path: Path) -> None:
    """list_threads 返回的 thread 若 JSONL 主存文件不存在 SHALL 跳过 + 发 thread_indexed_orphan。"""
    sink = _RecordingSink()
    directory = _new_directory(tmp_path, sink=sink)
    writer = JsonlMessageWriter(tmp_path / "threads")
    metas = await _seed_with_jsonl(directory, writer, count=3)

    # 删第 2 个 thread 的 JSONL 文件，模拟主存丢失
    deleted_tid = metas[1].thread_id
    (tmp_path / "threads" / f"{deleted_tid}.jsonl").unlink()

    page = await directory.list_threads(limit=10)
    returned_ids = {m.thread_id for m in page.items}
    assert deleted_tid not in returned_ids
    assert len(page.items) == 2

    orphan_events = [e for e in sink.events if e.msg.kind == "thread_indexed_orphan"]
    assert len(orphan_events) == 1
    assert orphan_events[0].msg.data["thread_id"] == deleted_tid
    await directory.close()
