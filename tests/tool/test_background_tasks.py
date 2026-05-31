"""BackgroundTaskRegistry + run_in_background / wait_for_task 测试
（M4 / tool-builtins-extended）。

覆盖 spec ``tool-builtins-extended`` 后两条 Requirement 的所有 Scenario。
"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins.background import (
    BackgroundTaskRegistry,
    make_run_in_background_tool,
    make_wait_for_task_tool,
)
from taifeng.tool.spec import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


# --------------------------------------------------------------------
# Registry —— Scenario: spawn + wait 完整生命周期
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_and_wait_completes() -> None:
    reg = BackgroundTaskRegistry()
    task_id = await reg.spawn("echo hello")
    result = await reg.wait(task_id, timeout=5.0)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    await reg.shutdown()


@pytest.mark.asyncio
async def test_spawn_returns_task_id_with_bg_prefix() -> None:
    reg = BackgroundTaskRegistry()
    task_id = await reg.spawn("true")
    assert task_id.startswith("bg_"), task_id
    # 等完成，避免 shutdown 时 race
    await reg.wait(task_id, timeout=2.0)
    await reg.shutdown()


# --------------------------------------------------------------------
# Registry —— Scenario: timeout 不杀进程
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_timeout_returns_status_timeout() -> None:
    reg = BackgroundTaskRegistry()
    task_id = await reg.spawn("sleep 2")
    result = await reg.wait(task_id, timeout=0.2)
    assert result["status"] == "timeout"
    assert result["exit_code"] is None
    # task 仍可被后续 kill
    await reg.kill(task_id)
    await reg.shutdown()


# --------------------------------------------------------------------
# Registry —— Scenario: shutdown / kill / max_concurrent
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_kills_all_tasks() -> None:
    reg = BackgroundTaskRegistry()
    ids = [await reg.spawn(f"sleep 30 # {i}") for i in range(3)]
    assert len(reg.list()) == 3
    await reg.shutdown()
    # shutdown 后再 wait 全部 unknown
    for tid in ids:
        result = await reg.wait(tid, timeout=0.5)
        assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_max_concurrent_enforced() -> None:
    reg = BackgroundTaskRegistry(max_concurrent=2)
    a = await reg.spawn("sleep 5")
    b = await reg.spawn("sleep 5")
    with pytest.raises(RuntimeError, match="too_many_background_tasks"):
        await reg.spawn("sleep 5")
    # 清理
    await reg.kill(a)
    await reg.kill(b)
    await reg.shutdown()


# --------------------------------------------------------------------
# Tool —— Scenario: 端到端 LLM 视角
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_end_to_end_long_task() -> None:
    reg = BackgroundTaskRegistry()
    spawn_tool = make_run_in_background_tool(registry=reg)
    wait_tool = make_wait_for_task_tool(registry=reg, default_timeout=5.0)

    r1 = await spawn_tool.handler(
        {"command": "sleep 0.3 && echo done"}, _ctx(),
    )
    assert not r1.is_error
    task_id = r1.data["task_id"]

    r2 = await wait_tool.handler(
        {"task_id": task_id, "timeout_seconds": 5.0}, _ctx(),
    )
    assert not r2.is_error
    assert r2.data["status"] == "completed"
    assert "done" in r2.data["stdout"]

    await reg.shutdown()


@pytest.mark.asyncio
async def test_tool_wait_timeout_returns_ok_with_status_timeout() -> None:
    """wait_for_task timeout 走 ok（is_error=False）让 LLM 自决。"""
    reg = BackgroundTaskRegistry()
    spawn_tool = make_run_in_background_tool(registry=reg)
    wait_tool = make_wait_for_task_tool(registry=reg)

    r1 = await spawn_tool.handler({"command": "sleep 5"}, _ctx())
    task_id = r1.data["task_id"]

    r2 = await wait_tool.handler(
        {"task_id": task_id, "timeout_seconds": 0.2}, _ctx(),
    )
    # 关键：is_error=False；让 LLM 用 data.status 路由
    assert not r2.is_error
    assert r2.data["status"] == "timeout"

    await reg.kill(task_id)
    await reg.shutdown()


@pytest.mark.asyncio
async def test_tool_safety_blacklist_blocks_dangerous() -> None:
    """run_in_background 复用 shell.py 的启发式 deny list。"""
    reg = BackgroundTaskRegistry()
    spawn_tool = make_run_in_background_tool(registry=reg)

    r = await spawn_tool.handler({"command": "rm -rf /"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "safety_blocked"

    await reg.shutdown()


@pytest.mark.asyncio
async def test_tool_unknown_task_id_returns_status_unknown() -> None:
    reg = BackgroundTaskRegistry()
    wait_tool = make_wait_for_task_tool(registry=reg)
    r = await wait_tool.handler({"task_id": "bg_ghost"}, _ctx())
    assert not r.is_error
    assert r.data["status"] == "unknown"
    await reg.shutdown()


# --------------------------------------------------------------------
# Concurrency —— 多并发 wait 同一 task_id
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_wait_same_task() -> None:
    """10 个 wait 并发同一个 task_id 不挂；都收到 completed。"""
    reg = BackgroundTaskRegistry()
    task_id = await reg.spawn("echo concurrent")

    async def _w() -> dict:
        return await reg.wait(task_id, timeout=3.0)

    results = await asyncio.gather(*[_w() for _ in range(10)])
    assert all(r["status"] == "completed" for r in results)
    assert all("concurrent" in r["stdout"] for r in results)

    await reg.shutdown()
