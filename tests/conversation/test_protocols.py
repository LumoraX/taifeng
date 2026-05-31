"""三协议（MessageWriter / ThreadDirectory / IndexHook）与零成本默认实现的契约测试。

覆盖：

- runtime_checkable: NoopIndexHook / NullThreadDirectory 通过 isinstance 校验
- 顶层导入: from taifeng import ... 全部新符号可导入
- frozen dataclass: 不可变性
- NullThreadDirectory 零结果约定
- NoopIndexHook 三方法可 await
"""

from __future__ import annotations

import dataclasses

import pytest

import taifeng
from taifeng.conversation import (
    IndexHook,
    MessageWriter,
    NoopIndexHook,
    NullThreadDirectory,
    RebuildReport,
    ThreadDirectory,
    ThreadFilter,
    ThreadMetadata,
    ThreadPage,
)


def test_protocol_runtime_checkable() -> None:
    """NoopIndexHook 与 NullThreadDirectory SHALL 通过 isinstance 校验对应协议。"""
    assert isinstance(NoopIndexHook(), IndexHook)
    assert isinstance(NullThreadDirectory(), ThreadDirectory)


def test_top_level_imports_available() -> None:
    """新增公共符号 SHALL 全部可从 taifeng 顶层导入。"""
    expected = {
        "MessageWriter",
        "ThreadDirectory",
        "IndexHook",
        "NoopIndexHook",
        "NullThreadDirectory",
        "ThreadMetadata",
        "ThreadFilter",
        "ThreadPage",
        "RebuildReport",
        "DirectoryError",
        "ThreadNotFoundError",
    }
    for name in expected:
        assert hasattr(taifeng, name), f"缺少顶层导出: {name}"


def test_thread_metadata_frozen() -> None:
    """ThreadMetadata SHALL 不可变（frozen dataclass）。"""
    meta = ThreadMetadata(
        thread_id="t1",
        created_at=1.0,
        updated_at=1.0,
        entry_skill_id="general",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.updated_at = 2.0  # type: ignore[misc]


def test_thread_filter_defaults_all_none() -> None:
    """ThreadFilter 默认全部字段为 None（不过滤语义）。"""
    f = ThreadFilter()
    assert f.entry_skill_id is None
    assert f.created_after is None
    assert f.created_before is None
    assert f.tag is None
    assert f.source is None


def test_thread_page_default_cursor_none() -> None:
    page = ThreadPage(items=[])
    assert page.items == []
    assert page.next_cursor is None


def test_rebuild_report_fields() -> None:
    r = RebuildReport(scanned_count=10, indexed_count=9, orphan_count=0, error_count=1, elapsed_ms=12.5)
    assert r.scanned_count == 10
    assert r.indexed_count == 9
    assert r.error_count == 1


def test_message_writer_is_protocol() -> None:
    """MessageWriter SHALL 是 runtime_checkable Protocol。"""

    # 自定义一个最小实现验证 isinstance 通过
    class Stub:
        async def create_thread(
            self,
            *,
            entry_skill_id: str,
            source: str = "user",
            tags: tuple[str, ...] = (),
            extra: dict | None = None,
        ) -> str:
            return "t1"

        async def append(self, thread_id, items):
            pass

        async def load_history(self, thread_id):
            return []

    assert isinstance(Stub(), MessageWriter)


@pytest.mark.asyncio
async def test_noop_index_hook_methods_callable() -> None:
    """NoopIndexHook 三方法 SHALL 可 await 且返回 None，无异常。"""
    hook = NoopIndexHook()
    meta = ThreadMetadata(
        thread_id="t1",
        created_at=0.0,
        updated_at=0.0,
        entry_skill_id="general",
    )
    assert await hook.on_thread_created(meta) is None
    assert await hook.on_message_appended("t1", []) is None
    assert await hook.on_metadata_updated("t1", {}) is None


@pytest.mark.asyncio
async def test_null_thread_directory_empty_results() -> None:
    """NullThreadDirectory.list_threads SHALL 返回空 page；get_metadata SHALL 返回 None。"""
    directory = NullThreadDirectory()
    page = await directory.list_threads()
    assert page.items == []
    assert page.next_cursor is None

    assert await directory.get_metadata("t1") is None
    # update / upsert 静默丢弃，不抛
    await directory.update_metadata("t1", {"x": 1})
    await directory.upsert_metadata(
        ThreadMetadata(thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g")
    )
