"""IndexHook 集成测试 —— HookRunner + EnginePool 接入。

覆盖 spec 5 个 Acceptance 用例：

- hook 被正确调用（create_thread / append / update_metadata）
- hook 失败不阻塞主路径 + 发 index_hook_failed
- shutdown grace period 内的 hook 等到完成
- shutdown 超出 grace 的 hook 被 cancel + 发 index_hook_abandoned
- 协议违反在构造期 raise TypeError
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.conversation import (
    IndexHook,
    NoopIndexHook,
    ResponseItem,
    ThreadMetadata,
    user_message,
)
from taifeng.conversation.hook_runner import HookRunner
from taifeng.loop.event import EventMsg


class _SpyHook:
    """记录所有调用 + 可控制延迟 / 抛错。"""

    def __init__(self, *, raise_on: str | None = None, sleep_seconds: float = 0.0) -> None:
        self.raise_on = raise_on
        self.sleep_seconds = sleep_seconds
        self.created: list[ThreadMetadata] = []
        self.appended: list[tuple[str, list[ResponseItem]]] = []
        self.updated: list[tuple[str, dict]] = []
        self.events: list[str] = []

    async def on_thread_created(self, meta: ThreadMetadata) -> None:
        self.events.append(f"create:{meta.thread_id}")
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.raise_on == "on_thread_created":
            raise RuntimeError("boom-create")
        self.created.append(meta)

    async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        self.events.append(f"append:{thread_id}")
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.raise_on == "on_message_appended":
            raise RuntimeError("boom-append")
        self.appended.append((thread_id, items))

    async def on_metadata_updated(self, thread_id: str, patch: dict) -> None:
        self.events.append(f"update:{thread_id}")
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.raise_on == "on_metadata_updated":
            raise RuntimeError("boom-update")
        self.updated.append((thread_id, patch))


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[EventMsg] = []

    async def handle(self, ev: EventMsg) -> None:
        self.events.append(ev)


# -----------------------------------------------------------------
# HookRunner 单元测试
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_called_on_create_append_update() -> None:
    """HookRunner SHALL 分别 spawn 三个方法，全部被调用。"""
    hook = _SpyHook()
    runner = HookRunner(hook=hook)
    meta = ThreadMetadata(
        thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g"
    )
    runner.spawn_on_thread_created(meta)
    runner.spawn_on_message_appended("t1", [user_message(text="hi", thread_id="t1")])
    runner.spawn_on_metadata_updated("t1", {"extra": {"x": 1}})

    await runner.shutdown(grace_seconds=2.0)
    assert len(hook.created) == 1
    assert len(hook.appended) == 1
    assert len(hook.updated) == 1


@pytest.mark.asyncio
async def test_hook_failure_emits_event_does_not_propagate() -> None:
    """hook 抛 RuntimeError SHALL 被捕获 + 发 index_hook_failed；调用方不受影响。"""
    hook = _SpyHook(raise_on="on_thread_created")
    sink = _RecordingSink()
    runner = HookRunner(hook=hook, sink=sink)
    meta = ThreadMetadata(
        thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g"
    )
    runner.spawn_on_thread_created(meta)  # 不抛
    await runner.shutdown(grace_seconds=2.0)  # 不抛

    failed = [e for e in sink.events if e.msg.kind == "index_hook_failed"]
    assert len(failed) == 1
    assert failed[0].msg.data["method"] == "on_thread_created"
    assert failed[0].msg.data["thread_id"] == "t1"
    assert "RuntimeError" in failed[0].msg.data["cause"]


@pytest.mark.asyncio
async def test_shutdown_waits_for_in_flight_hooks_within_grace() -> None:
    """grace period 内能完成的 hook SHALL 在 shutdown 返回前跑完。"""
    hook = _SpyHook(sleep_seconds=0.3)  # 0.3s < 5s grace
    runner = HookRunner(hook=hook)
    meta = ThreadMetadata(
        thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g"
    )
    runner.spawn_on_thread_created(meta)

    started = asyncio.get_event_loop().time()
    await runner.shutdown(grace_seconds=2.0)
    elapsed = asyncio.get_event_loop().time() - started

    assert 0.2 <= elapsed < 1.5, f"shutdown 应 ~0.3s 完成（含 hook 跑完），实际 {elapsed}s"
    assert len(hook.created) == 1  # hook 完成了


@pytest.mark.asyncio
async def test_shutdown_cancels_overrun_hooks_and_emits_abandoned() -> None:
    """grace period 超时的 hook SHALL 被 cancel + 发 index_hook_abandoned。"""
    hook = _SpyHook(sleep_seconds=10.0)  # 10s >> 0.3s grace
    sink = _RecordingSink()
    runner = HookRunner(hook=hook, sink=sink)
    meta = ThreadMetadata(
        thread_id="t1", created_at=0.0, updated_at=0.0, entry_skill_id="g"
    )
    runner.spawn_on_thread_created(meta)

    started = asyncio.get_event_loop().time()
    await runner.shutdown(grace_seconds=0.3)
    elapsed = asyncio.get_event_loop().time() - started

    assert 0.2 <= elapsed < 2.0, f"shutdown 应在 grace 后立即返回，实际 {elapsed}s"
    abandoned = [e for e in sink.events if e.msg.kind == "index_hook_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0].msg.data["method"] == "on_thread_created"
    assert abandoned[0].msg.data["thread_id"] == "t1"


def test_protocol_violation_raises_typeerror_at_construct() -> None:
    """传入不满足 IndexHook 协议的对象 SHALL 在构造期 raise TypeError。"""

    class NotAHook:
        # 只实现一个方法，缺另外两个
        async def on_thread_created(self, meta):
            pass

    with pytest.raises(TypeError, match="不满足 IndexHook 协议"):
        HookRunner(hook=NotAHook())  # type: ignore[arg-type]


def test_noop_hook_satisfies_protocol_and_constructs_runner() -> None:
    """NoopIndexHook 是合法实现，HookRunner 构造 SHALL 不抛。"""
    runner = HookRunner(hook=NoopIndexHook())
    # 不需要进一步断言，只要构造成功
    assert runner is not None


# -----------------------------------------------------------------
# EnginePool 集成测试
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_pool_integrates_index_hook(skills_dir: Path, threads_dir: Path) -> None:
    """EnginePool 接入 index_hook 后，get_or_create + 后续 append SHALL 触发 hook 调用。

    复用 conftest 全局 skills_dir / threads_dir fixture（含 code-reviewer 等已配 entry skill）。
    """
    from taifeng.llm.providers.mock import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage

    hook = _SpyHook()
    sink = _RecordingSink()
    client = MockClient(turns=[MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=2))])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,  # 旧名仍支持；新 storage_dir 等价
        model_client=client,
        compressors=[],
        index_hook=hook,
        sink=sink,
    )
    try:
        engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
        sub_id = await engine.submit(taifeng.UserMessage(text="hello"))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break
    finally:
        await pool.close()

    # 至少 create_thread + 一次 append 触发了 hook
    assert len(hook.created) >= 1, "on_thread_created should fire"
    assert len(hook.appended) >= 1, "on_message_appended should fire"
