"""分离式 skill spawn + join-barrier 测试。设计见
docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.spawn_handle import SpawnHandle, SpawnHandleRegistry


async def _wait(cond, tries: int = 200) -> bool:
    """轮询等待条件成立(每次 10ms,默认最多 2s),用于等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def test_registry_register_and_lookup() -> None:
    reg = SpawnHandleRegistry()
    h = reg.register(handle_id="sp0", skill_id="analyzer", child_thread_id="t-1")
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
        JoinBarrierFired, JoinBarrierRegistered, SpawnCancelled,
        SpawnCompleted, SpawnFailed, SpawnStarted, SpawnSuspended,
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
        join_barrier_fired_item, join_barrier_item, spawn_item,
    )
    si = spawn_item(handle_id="sp0", skill_id="a", child_thread_id="t1", thread_id="root")
    assert si.kind == "spawn" and si.payload["handle_id"] == "sp0"
    bi = join_barrier_item(barrier_id="b0", handle_ids=["sp0"],
                           then_skill_id="merge", then_args_template=None, thread_id="root")
    assert bi.kind == "join_barrier" and bi.payload["barrier_id"] == "b0"
    fi = join_barrier_fired_item(barrier_id="b0", then_thread_id="t9", thread_id="root")
    assert fi.kind == "join_barrier_fired"


@pytest.mark.asyncio
async def test_spawn_returns_handle_nonblocking(skills_dir, threads_dir) -> None:
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
    assert await _wait(
        lambda: engine.spawn_status([out["handle_id"]])[out["handle_id"]]["status"]
        == "done")
    assert (engine.spawn_status([out["handle_id"]])[out["handle_id"]]["result"]
            == "风格结论")
    await pool.close()
