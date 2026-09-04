"""JsonlMessageWriter（MessageWriter 协议默认实现）契约测试。

覆盖：

- create_thread 写入首行 ``__meta__`` 元数据（自包含）
- append + load_history 往返一致
- load_history 跳过损坏行 + 发 ``transcript_skipped_corrupt_line`` 事件
- 5 task 并发 append 无丢失 / 无撕裂
- iter_thread_files 枚举正确
- 协议 isinstance 校验
"""

from __future__ import annotations

import anyio
import json
import pytest
from pathlib import Path

from taifeng.conversation import (
    JsonlMessageWriter,
    MessageWriter,
    iter_thread_files,
    user_message,
)
from taifeng.loop.event import EventMsg


class _RecordingSink:
    """测试 sink —— 把所有 EventMsg 收集到 list 供断言。"""

    def __init__(self) -> None:
        self.events: list[EventMsg] = []

    async def handle(self, ev: EventMsg) -> None:
        self.events.append(ev)


def test_jsonl_writer_satisfies_protocol(tmp_path: Path) -> None:
    """JsonlMessageWriter 实例 SHALL 通过 isinstance(MessageWriter) 校验。"""
    writer = JsonlMessageWriter(tmp_path)
    assert isinstance(writer, MessageWriter)


@pytest.mark.asyncio
async def test_create_thread_first_line_is_metadata(tmp_path: Path) -> None:
    """create_thread 后 thread 文件首行 SHALL 是带 ``__meta__`` 标志的 metadata 行，
    且字段与构造参数对齐。"""
    writer = JsonlMessageWriter(tmp_path)
    tid = await writer.create_thread(
        entry_skill_id="general", source="user", tags=("production",), extra={"cwd": "/work"}
    )

    path = tmp_path / f"{tid}.jsonl"
    assert path.exists()
    with path.open("r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    meta = json.loads(first_line)
    assert meta["__meta__"] is True
    assert meta["thread_id"] == tid
    assert meta["entry_skill_id"] == "general"
    assert meta["source"] == "user"
    assert meta["tags"] == ["production"]
    assert meta["extra"] == {"cwd": "/work"}
    assert isinstance(meta["created_at"], float)
    assert isinstance(meta["updated_at"], float)


@pytest.mark.asyncio
async def test_append_load_history_roundtrip(tmp_path: Path) -> None:
    """append 写入后 load_history SHALL 完整回放且不含首行 metadata。"""
    writer = JsonlMessageWriter(tmp_path)
    tid = await writer.create_thread(entry_skill_id="general")

    items_in = [
        user_message(text="hi", thread_id=tid),
        user_message(text="hello again", thread_id=tid),
    ]
    await writer.append(tid, items_in)

    loaded = await writer.load_history(tid)
    assert len(loaded) == 2
    assert all(it.kind == "user_message" for it in loaded)
    assert [it.payload["text"] for it in loaded] == ["hi", "hello again"]


@pytest.mark.asyncio
async def test_load_history_skips_corrupt_line_with_event(tmp_path: Path) -> None:
    """中间一行被外部破坏（非 JSON）时，load_history SHALL 跳过且发事件，不抛。"""
    sink = _RecordingSink()
    writer = JsonlMessageWriter(tmp_path, sink=sink)
    tid = await writer.create_thread(entry_skill_id="general")
    await writer.append(tid, [user_message(text="good_1", thread_id=tid)])

    # 外部追加一行坏数据，再追加一条正常数据
    path = tmp_path / f"{tid}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("this is not valid json\n")
    await writer.append(tid, [user_message(text="good_2", thread_id=tid)])

    loaded = await writer.load_history(tid)
    texts = [it.payload["text"] for it in loaded]
    assert texts == ["good_1", "good_2"]  # 坏行被跳过，前后两条都在

    # 事件被发出
    corrupt_events = [e for e in sink.events if e.msg.kind == "transcript_skipped_corrupt_line"]
    assert len(corrupt_events) == 1
    assert corrupt_events[0].submission_id == "*"
    assert corrupt_events[0].msg.data["thread_id"] == tid


@pytest.mark.asyncio
async def test_load_history_without_sink_silent(tmp_path: Path) -> None:
    """sink 未注入时 load_history 跳过损坏行 SHALL 不抛异常（事件静默丢弃）。"""
    writer = JsonlMessageWriter(tmp_path)  # 无 sink
    tid = await writer.create_thread(entry_skill_id="general")
    path = tmp_path / f"{tid}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("garbage\n")
    loaded = await writer.load_history(tid)
    assert loaded == []


@pytest.mark.asyncio
async def test_load_history_nonexistent_thread_returns_empty(tmp_path: Path) -> None:
    """load_history 对不存在的 thread SHALL 返回空列表（不抛 FileNotFoundError）。"""
    writer = JsonlMessageWriter(tmp_path)
    assert await writer.load_history("nope") == []


@pytest.mark.asyncio
async def test_concurrent_append_no_lost_or_torn_lines(tmp_path: Path) -> None:
    """5 task 并发对同一 thread append（每条 < 4KB），load_history SHALL 含全部 5 条。"""
    writer = JsonlMessageWriter(tmp_path)
    tid = await writer.create_thread(entry_skill_id="general")

    async def _append_one(i: int) -> None:
        await writer.append(tid, [user_message(text=f"msg_{i}", thread_id=tid)])

    async with anyio.create_task_group() as tg:
        for i in range(5):
            tg.start_soon(_append_one, i)

    loaded = await writer.load_history(tid)
    assert len(loaded) == 5
    # 顺序不保证；只校验内容集合
    assert {it.payload["text"] for it in loaded} == {f"msg_{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_iter_thread_files_lists_all_threads(tmp_path: Path) -> None:
    """iter_thread_files SHALL 枚举所有 *.jsonl 文件且与 create_thread 路径一致。"""
    writer = JsonlMessageWriter(tmp_path)
    tids = [await writer.create_thread(entry_skill_id="general") for _ in range(3)]

    found = list(iter_thread_files(tmp_path))
    assert len(found) == 3
    found_names = {p.stem for p in found}
    assert set(tids) == found_names


def test_iter_thread_files_empty_dir(tmp_path: Path) -> None:
    """iter_thread_files 对空目录 SHALL 返回空迭代器，不抛。"""
    assert list(iter_thread_files(tmp_path)) == []


def test_iter_thread_files_nonexistent_dir(tmp_path: Path) -> None:
    """iter_thread_files 对不存在目录 SHALL 返回空迭代器，不抛。"""
    assert list(iter_thread_files(tmp_path / "does-not-exist")) == []
