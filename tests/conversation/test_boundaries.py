"""持久化层边界与并发测试 —— T7 (spec acceptance 6 用例)。

补强已有 test_sqlite_directory / test_index_hook 的覆盖盲点：

- limit 越界 / NullThreadDirectory 零副作用 / hook spawn 顺序 /
  ThreadNotFoundError / sqlite 不阻塞 event loop / 并发 append+list
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import anyio
import pytest

from taifeng.conversation import (
    JsonlMessageWriter,
    NullThreadDirectory,
    SqliteThreadDirectory,
    ThreadMetadata,
    ThreadNotFoundError,
    user_message,
)
from taifeng.conversation.hook_runner import HookRunner


# -----------------------------------------------------------------
# 1. limit 越界
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_validation_zero_and_too_large(tmp_path: Path) -> None:
    """list_threads(limit=0 / 1001) SHALL raise ValueError。"""
    directory = SqliteThreadDirectory(
        tmp_path / "taifeng-index.db",
        threads_dir=tmp_path / "threads",
    )
    with pytest.raises(ValueError):
        await directory.list_threads(limit=0)
    with pytest.raises(ValueError):
        await directory.list_threads(limit=1001)
    # 边界内允许
    await directory.list_threads(limit=1)
    await directory.list_threads(limit=1000)
    await directory.close()


# -----------------------------------------------------------------
# 2. NullThreadDirectory 零副作用
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_directory_no_file_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NullThreadDirectory 调任意方法 SHALL 不触达文件系统 / 不打开 sqlite 连接。"""

    import sqlite3
    import builtins

    open_calls = 0
    sqlite_calls = 0

    real_open = builtins.open
    real_connect = sqlite3.connect

    def spy_open(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal open_calls
        # 过滤 pytest 内部 open；仅监控显式 tmp_path 内的文件
        if args and str(args[0]).startswith(str(tmp_path)):
            open_calls += 1
        return real_open(*args, **kwargs)

    def spy_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal sqlite_calls
        sqlite_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    directory = NullThreadDirectory()
    await directory.list_threads()
    await directory.get_metadata("any-tid")
    await directory.update_metadata("any-tid", {"x": 1})
    await directory.upsert_metadata(
        ThreadMetadata(thread_id="any", created_at=0.0, updated_at=0.0, entry_skill_id="g")
    )

    assert open_calls == 0, f"NullThreadDirectory 不应触发 tmp_path 内的文件 open，实际 {open_calls}"
    assert sqlite_calls == 0, f"NullThreadDirectory 不应打开 sqlite 连接，实际 {sqlite_calls}"


# -----------------------------------------------------------------
# 3. hook spawn 顺序：create_thread 的 hook 先于 append 的 hook
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_spawn_order_create_before_append() -> None:
    """create_thread hook 完成时刻 SHALL ≤ first append hook 完成时刻（spawn 顺序保证）。"""

    spawn_records: list[tuple[str, float]] = []

    class _OrderHook:
        async def on_thread_created(self, meta: ThreadMetadata) -> None:
            spawn_records.append(("create", time.perf_counter()))

        async def on_message_appended(self, thread_id: str, items: list) -> None:  # type: ignore[type-arg]
            spawn_records.append(("append", time.perf_counter()))

        async def on_metadata_updated(self, thread_id: str, patch: dict) -> None:
            spawn_records.append(("update", time.perf_counter()))

    runner = HookRunner(hook=_OrderHook())
    meta = ThreadMetadata(
        thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g"
    )
    # 先 spawn create，再 spawn append（同一 thread 上的常见顺序）
    runner.spawn_on_thread_created(meta)
    runner.spawn_on_message_appended("t1", [user_message(text="hi", thread_id="t1")])
    await runner.shutdown(grace_seconds=2.0)

    # 两条记录都到位
    assert [r[0] for r in spawn_records] == ["create", "append"]
    # create 时间戳 ≤ append（spawn 顺序）
    assert spawn_records[0][1] <= spawn_records[1][1]


# -----------------------------------------------------------------
# 4. ThreadNotFoundError on update_metadata
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_not_found_error_on_update_metadata(tmp_path: Path) -> None:
    """update_metadata 不存在的 thread_id SHALL raise ThreadNotFoundError（与 T3 重复，T7 二次保险）。"""
    directory = SqliteThreadDirectory(
        tmp_path / "taifeng-index.db",
        threads_dir=tmp_path / "threads",
    )
    with pytest.raises(ThreadNotFoundError):
        await directory.update_metadata("never-existed", {"extra": {}})
    await directory.close()


# -----------------------------------------------------------------
# 5. sqlite 调用不阻塞 event loop
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_calls_run_in_thread_pool_no_loop_block(tmp_path: Path) -> None:
    """大量 sqlite upsert 同时跑，event loop 内的 ticker 任务 SHALL 仍然能进展。

    用 anyio.to_thread.run_sync 派发到 thread pool 是 spec 强制要求；如果实现退化为
    主线程同步 sqlite 调用，ticker_count 会大幅下降（被阻塞）。
    """
    writer = JsonlMessageWriter(tmp_path / "threads")
    directory = SqliteThreadDirectory(
        tmp_path / "taifeng-index.db",
        threads_dir=tmp_path / "threads",
    )

    # 准备 50 个 thread 文件（让 orphan 检查通过）
    for _ in range(50):
        await writer.create_thread(entry_skill_id="general")

    ticker_count = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticker_count
        while not stop:
            await anyio.sleep(0.002)
            ticker_count += 1

    ticker_task = asyncio.create_task(ticker())

    # 大批量 sqlite 调用
    now = time.time()
    for i in range(50):
        meta = ThreadMetadata(
            thread_id=f"thr_load_{i}",
            created_at=now,
            updated_at=now + i,
            entry_skill_id="general",
        )
        await directory.upsert_metadata(meta)

    stop = True
    await ticker_task

    # 若主 loop 完全被阻塞，ticker 一次都不会跑；这里要求至少跑了几次（容忍 CI 抖动）
    assert ticker_count >= 3, f"event loop 似乎被 sqlite 阻塞了（ticker 仅跑 {ticker_count} 次）"
    await directory.close()


# -----------------------------------------------------------------
# 6. 并发 append + list 不出错
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_append_with_concurrent_list(tmp_path: Path) -> None:
    """append 与 list_threads 同时跑 SHALL 不出错；最终 load_history 含全部 append 数据。"""
    writer = JsonlMessageWriter(tmp_path / "threads")
    directory = SqliteThreadDirectory(
        tmp_path / "taifeng-index.db",
        threads_dir=tmp_path / "threads",
    )

    # 准备 3 个 thread
    tids = [await writer.create_thread(entry_skill_id="general") for _ in range(3)]
    now = time.time()
    for i, tid in enumerate(tids):
        await directory.upsert_metadata(
            ThreadMetadata(
                thread_id=tid,
                created_at=now,
                updated_at=now + i,
                entry_skill_id="general",
            )
        )

    target = tids[0]

    async def appender() -> None:
        for i in range(15):
            await writer.append(target, [user_message(text=f"m{i}", thread_id=target)])

    async def lister() -> None:
        for _ in range(15):
            page = await directory.list_threads(limit=10)
            # 仅校验不抛 + 数量合理
            assert len(page.items) == 3

    async with anyio.create_task_group() as tg:
        tg.start_soon(appender)
        tg.start_soon(lister)

    history = await writer.load_history(target)
    assert len(history) == 15
    await directory.close()
