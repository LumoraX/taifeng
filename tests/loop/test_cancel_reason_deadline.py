"""cancel-reason-deadline —— 取消原因级联 + 墙钟截止时间。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken, CancelReason
from taifeng.loop.submission import CancelTurn
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def test_reason_cascades_to_descendants() -> None:
    """取消原因与说明级联到全部后代；首次取消的原因生效。"""
    root = CancellationToken(name="r")
    child = root.child("c")
    grandchild = child.child("g")
    root.cancel(CancelReason.SHUTDOWN, "pool_close")
    root.cancel(CancelReason.REQUESTED)  # 重复取消不改写原因
    for token in (root, child, grandchild):
        assert token.reason is CancelReason.SHUTDOWN
        assert token.detail == "pool_close"


def test_default_reason_is_requested_and_child_of_cancelled_parent_inherits() -> None:
    """无参 cancel = REQUESTED；已取消父派生的子立即以父原因取消。"""
    root = CancellationToken()
    root.cancel()
    assert root.reason is CancelReason.REQUESTED
    late = root.child("late")
    assert late.is_cancelled and late.reason is CancelReason.REQUESTED


def test_uncancelled_token_has_no_reason() -> None:
    token = CancellationToken()
    assert token.reason is None and token.detail is None


async def test_deadline_cancels_subtree_with_deadline_reason() -> None:
    """到点以 DEADLINE_EXCEEDED 取消本 token 与子树；兄弟不受影响。"""
    root = CancellationToken()
    limited = root.child("limited", deadline_seconds=0.05)
    inner = limited.child("inner")
    sibling = root.child("sibling")
    await asyncio.wait_for(inner.wait_cancelled(), timeout=GUARD_TIMEOUT_SECONDS)
    assert inner.reason is CancelReason.DEADLINE_EXCEEDED
    assert not sibling.is_cancelled and not root.is_cancelled


async def test_deadline_only_tightens() -> None:
    """重复设置只取更早者；放宽无效。"""
    token = CancellationToken()
    token.set_deadline(0.05)
    token.set_deadline(60)
    remaining = token.deadline_remaining()
    assert remaining is not None and remaining <= 0.05
    await asyncio.wait_for(token.wait_cancelled(), timeout=GUARD_TIMEOUT_SECONDS)


async def test_deadline_remaining_takes_earliest_ancestor() -> None:
    """deadline_remaining 取祖先链上最早的截止时间；无截止时间为 None。"""
    assert CancellationToken().deadline_remaining() is None
    root = CancellationToken()
    root.set_deadline(30)
    child = root.child("c", deadline_seconds=60)
    remaining = child.deadline_remaining()
    assert remaining is not None and remaining <= 30


async def test_cancel_before_deadline_disarms_timer() -> None:
    """先被主动取消 → 到期定时器作废，原因保持 REQUESTED。"""
    token = CancellationToken()
    token.set_deadline(0.05)
    token.cancel()
    await asyncio.sleep(0.1)
    assert token.reason is CancelReason.REQUESTED


@pytest.mark.parametrize("seconds", [0, -1])
async def test_non_positive_deadline_rejected(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        CancellationToken().set_deadline(seconds)


def test_user_message_rejects_non_positive_deadline() -> None:
    with pytest.raises(ValueError):
        taifeng.UserMessage(text="x", deadline_seconds=0)


async def _root_terminal(engine, sub_id: str) -> dict:
    """等 submission 的根 turn 终态事件 data。"""
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root", True):
            return {"kind": ev.msg.kind, **ev.msg.data}
    raise AssertionError("no terminal event")


async def test_turn_deadline_cancels_with_deadline_reason(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """UserMessage.deadline_seconds 到点 → turn 以 cancel_reason=deadline_exceeded 终止。"""
    client = SimClient(turns=[SimTurn(text="很慢的回答", delay_seconds=5.0)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="dl", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="hi", deadline_seconds=0.1))
    data = await asyncio.wait_for(_root_terminal(engine, sub_id), timeout=GUARD_TIMEOUT_SECONDS)
    assert data["end_reason"] == "cancelled"
    assert data["cancel_reason"] == "deadline_exceeded"
    await pool.close()


async def test_cancel_turn_reports_requested_reason(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """CancelTurn → cancel_reason=requested（与超时区分）。"""
    client = SimClient(turns=[SimTurn(text="很慢的回答", delay_seconds=5.0)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="ct", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="hi"))
    await wait_for_condition(lambda: sub_id in engine._pending)  # noqa: SLF001
    await engine.submit(CancelTurn(submission_id=sub_id))
    data = await asyncio.wait_for(_root_terminal(engine, sub_id), timeout=GUARD_TIMEOUT_SECONDS)
    assert data["end_reason"] == "cancelled"
    assert data["cancel_reason"] == "requested"
    await pool.close()


async def test_spawn_deadline_cancels_spawn_subtree(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """spawn_skill(deadline_seconds=) → 子树到点取消，句柄进入终态。"""
    from taifeng.llm.providers.sim import RoutingSimClient

    client = RoutingSimClient(routes={
        "style-checker": [SimTurn(text="很慢的风格结论", delay_seconds=5.0)],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="sp", entry_skill_id="code-reviewer")
    out = await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="并发分析", deadline_seconds=0.1)
    hid = out["handle_id"]
    await wait_for_condition(
        lambda: engine.spawn_status([hid])[hid]["status"] != "running")
    assert engine.spawn_status([hid])[hid]["status"] == "cancelled"
    await pool.close()


async def test_interrupt_on_cancel_breaks_blocking_await() -> None:
    """token 取消原地打断块内阻塞的 await，抛出带原因的 CancelledError。"""
    from taifeng.loop.cancellation import interrupt_on_cancel

    token = CancellationToken(name="turn")
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, token.cancel, CancelReason.DEADLINE_EXCEEDED)
    started = loop.time()
    with pytest.raises(asyncio.CancelledError, match="deadline_exceeded"):
        async with interrupt_on_cancel(token):
            await asyncio.sleep(10)
    assert loop.time() - started < 1.0
    # 由 token 引起的 task 取消已被 uncancel：当前 task 不残留取消请求
    assert asyncio.current_task().cancelling() == 0


async def test_interrupt_on_cancel_keeps_external_cancellation() -> None:
    """外部 task.cancel 不被改写为 token 取消（照常外抛，K5 语义）。"""
    from taifeng.loop.cancellation import interrupt_on_cancel

    token = CancellationToken()

    async def _body() -> None:
        async with interrupt_on_cancel(token):
            await asyncio.sleep(10)

    task = asyncio.create_task(_body())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not token.is_cancelled
