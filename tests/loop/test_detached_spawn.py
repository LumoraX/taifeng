"""分离式 skill spawn + join-barrier 测试。设计见
docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.spawn_handle import SpawnHandleRegistry


async def _wait(cond, tries: int = 200) -> bool:
    """轮询等待条件成立(每次 10ms,默认最多 2s),用于等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def test_registry_register_and_lookup() -> None:
    reg = SpawnHandleRegistry()
    h = reg.register(
        handle_id="sp0", skill_id="analyzer", child_thread_id="t-1"
    )
    assert h.status == "running"
    assert reg.get("sp0") is h
    assert reg.get("nope") is None


def test_registry_set_status_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="sp0", skill_id="a", child_thread_id="t-1")
    reg.set_result("sp0", status="done", result="结论A")
    h = reg.get("sp0")
    assert h.status == "done" and h.result == "结论A"
    assert reg.is_terminal("sp0")


def test_registry_all_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="a", skill_id="x", child_thread_id="t1")
    reg.register(handle_id="b", skill_id="x", child_thread_id="t2")
    reg.set_result("a", status="done", result="ra")
    assert not reg.all_terminal(["a", "b"])
    reg.set_result("b", status="error", result="boom")
    assert reg.all_terminal(["a", "b"])  # done + error 都算终态


def test_spawn_event_kinds() -> None:
    from taifeng.loop.event import (
        JoinBarrierFired,
        JoinBarrierRegistered,
        SpawnCancelled,
        SpawnCompleted,
        SpawnFailed,
        SpawnStarted,
        SpawnSuspended,
    )
    assert SpawnStarted().kind == "spawn_started"
    assert SpawnSuspended().kind == "spawn_suspended"
    assert SpawnCompleted().kind == "spawn_completed"
    assert SpawnFailed().kind == "spawn_failed"
    assert SpawnCancelled().kind == "spawn_cancelled"
    assert JoinBarrierRegistered().kind == "join_barrier_registered"
    assert JoinBarrierFired().kind == "join_barrier_fired"


def test_spawn_response_items() -> None:
    from taifeng.conversation.models import (
        join_barrier_fired_item,
        join_barrier_item,
        spawn_item,
    )
    si = spawn_item(
        handle_id="sp0", skill_id="a",
        child_thread_id="t1", thread_id="root",
    )
    assert si.kind == "spawn" and si.payload["handle_id"] == "sp0"
    bi = join_barrier_item(
        barrier_id="b0", handle_ids=["sp0"],
        then_skill_id="merge", then_args_template=None,
        thread_id="root",
    )
    assert bi.kind == "join_barrier" and bi.payload["barrier_id"] == "b0"
    fi = join_barrier_fired_item(
        barrier_id="b0", then_thread_id="t9", thread_id="root"
    )
    assert fi.kind == "join_barrier_fired"


@pytest.mark.asyncio
async def test_spawn_returns_handle_nonblocking(
    skills_dir, threads_dir
) -> None:
    """engine.spawn_skill 立即返回句柄(非阻塞),后台分离 task 跑完后句柄转 done。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="风格结论")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer")
    out = await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="并发分析")
    assert out["handle_id"] and out["child_thread_id"]
    # 句柄立即可见(running),后台 task 跑完后转 done
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )
    assert engine.spawn_status([hid])[hid]["result"] == "风格结论"
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_seed_id_consistent_store_vs_memory(
    skills_dir, threads_dir
) -> None:
    """C1：store 里落盘的种子 id 与 history_buffer[0] 使用的 id 一致。

    修复前：_drive_spawn 会重建 seed（新随机 id），导致 store 里 id ≠
    内存中 id，冷恢复时会重建出不同的消息图谱。
    修复后：seed 在 spawn_skill 构造一次，直接传入 _drive_spawn /
    _build_child_runner，两路径共享同一对象，id 天然一致。
    此测试验证子 thread 的首条记录是 user_message 且内容与 args 对应。
    """
    import json

    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="风格结论2")],
        "code-reviewer": [MockTurn(text="主2")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s-c1", entry_skill_id="code-reviewer")

    args_payload = {"key": "value"}
    out = await engine.spawn_skill(
        skill_id="style-checker", args=args_payload, reason="c1-test")

    # 等子 task 完成确保 store 已 flush
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )

    # 从 store 读回 child thread，首条必须是 user_message 且内容与 args 一致
    child_tid = out["child_thread_id"]
    thread_iter = await pool.store.load_thread(child_tid)
    items = [item async for item in thread_iter]
    assert items, "子 thread 至少有一条记录"
    first = items[0]
    assert first.kind == "user_message", (
        f"首条应为 user_message，实际: {first.kind}"
    )
    seed_text = first.payload["text"]
    assert json.loads(seed_text) == args_payload, (
        f"种子内容不匹配: {seed_text}"
    )
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_k1_slot_released_on_create_thread_failure(
    skills_dir, threads_dir
) -> None:
    """C2：create_thread 失败时 K1 spawn 槽位必须释放，不永久泄漏。

    修复前：reserve_manual 与 create_task 之间若抛出，_drive_spawn 从不
    启动，其 finally 中的 release_manual 永远不执行 → 槽位泄漏。
    修复后：spawn_skill 在 try/except 里捕获预启动失败，立即
    release_manual 后重抛。
    """
    from unittest.mock import AsyncMock, patch

    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="ok")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s-c2", entry_skill_id="code-reviewer")

    # 注入：让 store.create_thread 抛出，模拟预启动失败
    with patch.object(
        engine._store, "create_thread",
        new_callable=AsyncMock,
        side_effect=RuntimeError("模拟 create_thread 失败"),
    ), pytest.raises(RuntimeError, match="模拟 create_thread 失败"):
        await engine.spawn_skill(
            skill_id="style-checker", args={}, reason="c2-test")

    # 槽位必须已被释放（_active 回到 0）
    snap = engine._spawn_registry.snapshot()
    assert snap["active"] == 0, f"K1 槽位泄漏，active={snap['active']}"

    # 泄漏修复后，后续正常 spawn 依然可以成功
    out = await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="c2-後続")
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )
    await pool.close()


@pytest.mark.asyncio
async def test_same_skill_multiple_instances(skills_dir, threads_dir):
    """同一 skill 同时 spawn 三个实例，各自拥有独立句柄和独立 child thread。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="路线1"), MockTurn(text="路线2"), MockTurn(text="路线3")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    handles = [
        (await engine.spawn_skill(skill_id="style-checker", args={"i": i}, reason="路线"))["handle_id"]
        for i in range(3)
    ]
    assert len(set(handles)) == 3  # 三个独立句柄
    assert await _wait(lambda: all(
        engine.spawn_status([h])[h]["status"] == "done" for h in handles))
    # 三条独立 child thread
    threads = {engine._spawn_handles.get(h).child_thread_id for h in handles}  # noqa: SLF001
    assert len(threads) == 3
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_independent_completion_events(skills_dir, threads_dir):
    """两个独立 spawn 各自发出独立的 spawn_completed 事件，handle_id 互不混淆。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A结论"), MockTurn(text="B结论")],
        "code-reviewer": [MockTurn(text="主")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s2b", entry_skill_id="code-reviewer")
    completed: dict[str, str] = {}

    async def watch():
        """订阅所有事件，收集 spawn_completed 直到收到两条为止。"""
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_completed":
                completed[ev.msg.data["handle_id"]] = ev.msg.data.get("result", "")
                if len(completed) >= 2:
                    return

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="a"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="b"))["handle_id"]
    assert await _wait(lambda: len(completed) >= 2)
    assert set(completed.keys()) == {a, b}  # 各自独立 spawn_completed 事件
    task.cancel()
    await pool.close()
