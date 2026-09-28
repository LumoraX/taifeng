"""usage-tree-accounting —— 整棵 turn 树共享会话计量器。

回归点：此前只有根 turn 收尾时把自身 usage 加进会话累计，call_skill 子树的 usage
从不回灌，K2 ``max_session_tokens`` 可被子树绕过。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage, add_usage
from taifeng.loop.usage_meter import SessionUsageMeter
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def _u(total: int) -> TokenUsage:
    """构造一份 input=total、output=0 的 usage。"""
    return TokenUsage(input_tokens=total, output_tokens=0, total_tokens=total)


def _delegating_client(child_tokens: int) -> SimClient:
    """父派发 style-checker（100）→ 子采样（child_tokens）→ 父收尾（100）。"""
    return SimClient(turns=[
        SimTurn(text="", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "检查风格"}',
        }], usage=_u(100)),
        SimTurn(text="风格没问题", usage=_u(child_tokens)),
        SimTurn(text="审查完成", usage=_u(100)),
    ])


async def _run_turn(engine, text: str) -> list:
    """提交并经 subscribe_all 收集到根 turn 终态（或拒绝）为止的事件。

    不用 ``subscribe(sub_id)``：它在该 submission 的首个 turn_completed（可能是子
    turn 的）处结束，拿不到根 turn 的收尾事件。
    """
    msgs: list = []
    done = asyncio.Event()

    async def _collect() -> None:
        async for ev in engine.subscribe_all():
            msgs.append(ev.msg)
            if ev.msg.kind in ("turn_failed", "turn_refused", "resource_limit_exceeded") or (
                ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
            ):
                done.set()
                return

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text=text))
    await asyncio.wait_for(done.wait(), timeout=GUARD_TIMEOUT_SECONDS)
    task.cancel()
    return msgs


def test_session_usage_meter_add_attributes_by_skill_and_thread() -> None:
    """计量器累加总量，并按 skill / thread 分别归因。"""
    meter = SessionUsageMeter()
    meter.add(_u(100), thread_id="t_root", skill_id="parent")
    meter.add(_u(300), thread_id="t_child", skill_id="child")
    meter.add(TokenUsage(input_tokens=10, output_tokens=5), thread_id="t_root", skill_id="parent")

    assert meter.total_tokens == 415  # total 缺省按 input + output 补
    snap = meter.snapshot()
    assert snap["by_skill"]["parent"]["total_tokens"] == 115
    assert snap["by_skill"]["parent"]["samples"] == 2
    assert snap["by_skill"]["child"]["total_tokens"] == 300
    assert meter.thread_total("t_child") == 300
    assert meter.thread_total("unknown") == 0


def test_add_usage_sums_fields_and_fills_total() -> None:
    """add_usage 逐字段相加，缺 total 时按 input + output 补。"""
    merged = add_usage(_u(10), TokenUsage(input_tokens=3, output_tokens=2, reasoning_tokens=1))
    assert merged.total_tokens == 15
    assert merged.input_tokens == 13
    assert merged.reasoning_tokens == 1


async def test_call_skill_child_usage_counts_toward_session(
    skills_dir: Path, threads_dir: Path
) -> None:
    """子 skill 采样用量进入会话累计与根 turn 的 subtree_usage，且按 skill 归因。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=_delegating_client(3000), compressors=[])
    engine = await pool.get_or_create(session_id="u1", entry_skill_id="code-reviewer")

    msgs = await _run_turn(engine, "审查这段代码")

    root_done = [m for m in msgs if m.kind == "turn_completed" and m.data.get("is_root")]
    assert root_done, [m.kind for m in msgs]
    data = root_done[-1].data
    assert data["usage"]["total_tokens"] == 200          # 根自身两次采样
    assert data["subtree_usage"]["total_tokens"] == 3200  # 含阻塞子树
    assert data["skill_id"] == "code-reviewer"
    assert data["thread_id"] == engine.thread_id

    assert engine._session_tokens == 3200  # noqa: SLF001
    by_skill = engine.introspect()["usage"]["by_skill"]
    assert by_skill["style-checker"]["total_tokens"] == 3000
    assert by_skill["code-reviewer"]["total_tokens"] == 200
    await pool.close()


async def test_child_usage_trips_session_limit_for_next_turn(
    skills_dir: Path, threads_dir: Path
) -> None:
    """根自身只用 200，但子树用 3000 → 会话已过 1000 上限，下一轮被 K2 拒绝。"""
    # 第二轮若被放行会取到脚本外采样并失败；被 K2 拒绝才是正确结局
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=_delegating_client(3000), compressors=[], max_session_tokens=1000)
    engine = await pool.get_or_create(session_id="u2", entry_skill_id="code-reviewer")

    await _run_turn(engine, "第一轮")
    assert engine._session_tokens >= 1000  # noqa: SLF001

    second = await _run_turn(engine, "第二轮")
    kinds = [m.kind for m in second]
    assert "turn_refused" in kinds or "resource_limit_exceeded" in kinds, kinds
    await pool.close()


async def test_detached_spawn_usage_counts_toward_session(
    skills_dir: Path, threads_dir: Path
) -> None:
    """detached spawn 子树的采样用量实时进入会话累计（此前从不回灌 engine 计量）。"""
    from taifeng.llm.providers.sim import RoutingSimClient

    client = RoutingSimClient(routes={
        "style-checker": [SimTurn(text="风格结论", usage=_u(4000))],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="u3", entry_skill_id="code-reviewer")

    out = await engine.spawn_skill(skill_id="style-checker", args={}, reason="并发分析")
    hid = out["handle_id"]
    await wait_for_condition(lambda: engine.spawn_status([hid])[hid]["status"] == "done")

    assert engine._session_tokens == 4000  # noqa: SLF001
    usage = engine.introspect()["usage"]
    assert usage["by_skill"]["style-checker"]["total_tokens"] == 4000
    assert usage["by_thread"][out["child_thread_id"]]["total_tokens"] == 4000
    await pool.close()
