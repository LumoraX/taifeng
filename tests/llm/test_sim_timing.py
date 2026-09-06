"""SimCoordinator 单测 —— 确定性并发时序编排（await_signal / emit_signal）。"""

from __future__ import annotations

import asyncio

from taifeng.llm.providers.sim import RoutingSimClient, SimCoordinator, SimTurn
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken
from tests.conftest import GUARD_TIMEOUT_SECONDS


def _req(text: str) -> ApiRequest:
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content=text)])


async def _drain_text(client: RoutingSimClient, text: str) -> str:
    out = ""
    async with client.session(cancel=CancellationToken()) as s:
        async for ev in s.stream(_req(text)):
            if ev.kind == "text_delta":
                out += ev.data.get("text", "")
    return out


async def test_coordinator_signal_then_wait_any_order():
    """先 signal 后 wait / 先 wait 后 signal 均成立（Event 幂等）。"""
    coord = SimCoordinator()
    coord.signal("done")
    await asyncio.wait_for(coord.wait("done"), timeout=GUARD_TIMEOUT_SECONDS)

    coord2 = SimCoordinator()
    waiter = asyncio.create_task(coord2.wait("later"))
    await asyncio.sleep(0)
    coord2.signal("later")
    await asyncio.wait_for(waiter, timeout=GUARD_TIMEOUT_SECONDS)


async def test_orchestrated_completion_order():
    """显式编排并发完成顺序：A 等 B 的信号 → B 必然先完成（确定性、零随机）。"""
    client = RoutingSimClient(routes={
        "TRACK_A": [SimTurn(text="A-done", await_signal="b-finished")],
        "TRACK_B": [SimTurn(text="B-done", emit_signal="b-finished")],
    })
    order: list[str] = []

    async def run(track: str) -> None:
        await _drain_text(client, f"run {track}")
        order.append(track)

    # 同时起跑：A 先被调度也必须等 B 点亮信号
    await asyncio.wait_for(
        asyncio.gather(run("TRACK_A"), run("TRACK_B")), timeout=GUARD_TIMEOUT_SECONDS
    )
    assert order == ["TRACK_B", "TRACK_A"]


async def test_emit_signal_fires_before_completed():
    """emit_signal 在 completed 之前点亮：等待方可在发起方终态前被放行。"""
    coord = SimCoordinator()
    client = RoutingSimClient(
        routes={"R": [SimTurn(text="x", emit_signal="mid")]},
        coordinator=coord,
    )
    await _drain_text(client, "R")
    # 「completed 之前就已点亮」是本用例的断言本体 —— 用**同步查 Event 状态**判定，
    # 不用 `wait_for(..., timeout=0.1)`：那种写法拿墙钟当证据，期限与调度抖动同
    # 数量级会误报；而放宽期限又会把断言变成恒真（信号晚点亮也照样通过）。
    assert coord._event("mid").is_set(), (  # noqa: SLF001
        "emit_signal 必须在 completed 之前点亮，drain 结束时信号应已就绪"
    )


async def test_reset_clears_signals():
    coord = SimCoordinator()
    coord.signal("s")
    coord.reset()
    waiter = asyncio.create_task(coord.wait("s"))
    await asyncio.sleep(0.01)
    assert not waiter.done()  # reset 后旧信号失效
    coord.signal("s")
    await asyncio.wait_for(waiter, timeout=GUARD_TIMEOUT_SECONDS)
