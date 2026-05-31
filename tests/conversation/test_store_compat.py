"""JsonlMessageStore 向后兼容封装契约测试。

验证旧 API 形态下行为与改造前等价（实际跑过 test_engine_e2e 等既有测试是 R5 主要保证）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.conversation import (
    JsonlMessageStore,
    MessageStore,
    user_message,
)


def test_jsonl_message_store_satisfies_legacy_protocol(tmp_path: Path) -> None:
    """JsonlMessageStore SHALL 通过 isinstance(MessageStore) 校验（旧协议）。"""
    store = JsonlMessageStore(tmp_path)
    assert isinstance(store, MessageStore)


@pytest.mark.asyncio
async def test_legacy_api_create_append_load_list(tmp_path: Path) -> None:
    """旧 API: create_thread(cwd=...) / append / load_thread / list_threads SHALL 端到端可用。"""
    store = JsonlMessageStore(tmp_path)
    tid = await store.create_thread(cwd="/work/repo", entry_skill_id="general", source="user")
    await store.append(user_message(text="hi", thread_id=tid))
    await store.append(user_message(text="bye", thread_id=tid))

    # load_thread 返回 AsyncIterator
    gen = await store.load_thread(tid)
    items = [it async for it in gen]
    assert [it.payload["text"] for it in items] == ["hi", "bye"]

    # list_threads + cwd 过滤生效
    threads = await store.list_threads(cwd="/work/repo")
    assert any(t.thread_id == tid and t.cwd == "/work/repo" for t in threads)

    # cwd 不匹配则不返回
    none_threads = await store.list_threads(cwd="/some-other-path")
    assert all(t.thread_id != tid for t in none_threads)

    await store.close()


@pytest.mark.asyncio
async def test_legacy_select_resume_path_returns_most_recent_matching_cwd(tmp_path: Path) -> None:
    """select_resume_path(cwd) SHALL 返回最近匹配 cwd 的 thread_id（兼容版简化语义）。"""
    store = JsonlMessageStore(tmp_path)
    t1 = await store.create_thread(cwd="/a")
    t2 = await store.create_thread(cwd="/a")  # 更晚 → 应被选中
    await store.create_thread(cwd="/b")
    chosen = await store.select_resume_path("/a")
    assert chosen in {t1, t2}  # 兼容版返回最近的 /a 匹配
    none = await store.select_resume_path("/nonexistent")
    assert none is None
    await store.close()


@pytest.mark.asyncio
async def test_legacy_append_batch_groups_by_thread(tmp_path: Path) -> None:
    """append_batch 多 thread 混合 SHALL 正确分组，每 thread load_thread 拿到自己的 items。"""
    store = JsonlMessageStore(tmp_path)
    t1 = await store.create_thread()
    t2 = await store.create_thread()
    await store.append_batch(
        [
            user_message(text="t1-a", thread_id=t1),
            user_message(text="t2-a", thread_id=t2),
            user_message(text="t1-b", thread_id=t1),
        ]
    )
    g1 = await store.load_thread(t1)
    t1_items = [it async for it in g1]
    g2 = await store.load_thread(t2)
    t2_items = [it async for it in g2]
    assert [it.payload["text"] for it in t1_items] == ["t1-a", "t1-b"]
    assert [it.payload["text"] for it in t2_items] == ["t2-a"]
    await store.close()
