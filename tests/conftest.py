"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ATOMIC_SKILL = """---
name: style-checker
description: 代码风格审查
version: 1.0.0
type: atomic
---
# 风格审查
按规范审查 diff，列出违规处。
"""

COMPOSITE_SKILL = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是一位代码审查专家。
"""


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(ATOMIC_SKILL, encoding="utf-8")
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE_SKILL, encoding="utf-8")
    return skills


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    p = tmp_path / "threads"
    p.mkdir()
    return p


@pytest.fixture
def sim_client():
    """SimClient/RoutingSimClient 工厂 fixture —— 收尾自动断言无合同违规。

    D4 双保险：即使 ``SimContractViolation`` 被引擎兜底路径吞掉转成 turn_failed，
    teardown 的 violations 断言仍能让测试红。

    用法：
        client = sim_client(turns=[SimTurn(...)])              # 顺序回放
        client = sim_client(routes={"MARK": [SimTurn(...)]})   # 标记路由
    """
    created: list = []

    def factory(*, turns=None, routes=None, **kwargs):
        from taifeng.llm.providers.sim import RoutingSimClient, SimClient

        if routes is not None:
            client = RoutingSimClient(routes=routes, **kwargs)
        else:
            client = SimClient(turns=list(turns or []), **kwargs)
        created.append(client)
        return client

    yield factory
    for client in created:
        leftovers = [str(v) for v in client.ledger.violations]
        assert not leftovers, f"sim 合同违规未处理: {leftovers}"


# ---------------------------------------------------------------------------
# 根 turn 终态等待 —— 单一出处
# ---------------------------------------------------------------------------


async def run_until_root_done(
    engine: Any,
    op: Any,
    *,
    deadline_seconds: float = 10.0,
) -> list[Any]:
    """提交 ``op`` 并等**最外层根 turn** 终态，返回该 submission 的全部事件。

    这是端到端测试等待 turn 收敛的**唯一正确姿势**。两个叠加的坑让手写版本
    反复出错，故收敛到此处，勿再各自实现：

    1. ``call_skill`` 派生的子 turn **复用父的 submission_id**，且比父更早
       emit ``turn_completed``；把首个终态当作结束会让父 turn 停在结算前，
       随后 ``pool.close()`` 取消它 —— 父侧 fc/fco 永不落盘，测出来的 history
       是残缺态，而不依赖这些条目的断言照样通过（静默测错东西）。
    2. ``engine.subscribe(sub_id)`` 在首个终态事件后即**关流**，之后的根终态
       根本收不到。故必须用 ``subscribe_all()`` 自行过滤 submission_id。

    判据取 ``data["is_root"]`` —— 这是 ``loop/event.py`` 对 ``TurnCompleted`` /
    ``TurnFailed`` 的明文契约（「消费方应当只在 is_root=True 时认为本 submission
    已结束」），三个终态 emit 点均带该字段。不要用 skill_dispatched/returned
    计数深度间接推断。

    Args:
        engine: 目标 ``AgentEngine``。
        op: 要提交的 Op（通常是 ``taifeng.UserMessage``）。
        deadline_seconds: 超时上限；超时即测试失败，不静默挂死。

    Returns:
        该 submission 的全部事件（保序），末条为根终态事件。
    """
    import asyncio
    import contextlib

    events: list[Any] = []
    sub_holder: list[str] = []
    done = asyncio.Event()

    async def collector() -> None:
        async for ev in engine.subscribe_all():
            if not sub_holder or ev.submission_id != sub_holder[0]:
                continue
            events.append(ev)
            if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get(
                "is_root"
            ):
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)  # 让 collector 先注册 subscribe_all 队列
    sub_holder.append(await engine.submit(op))
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    assert events, "未收到任何事件"
    return events


async def run_until_root_done_kind(
    engine: Any,
    op: Any,
    *,
    deadline_seconds: float = 10.0,
) -> str:
    """``run_until_root_done`` 的便捷包装：只要根终态的 kind。"""
    events = await run_until_root_done(engine, op, deadline_seconds=deadline_seconds)
    return str(events[-1].msg.kind)

TURN_TERMINAL_KINDS = ("turn_suspended", "turn_completed", "turn_failed")


def last_turn_terminal(events: list[Any]) -> str | None:
    """取一批事件里**最后一条 turn 终态**的 kind（挂起 / 完成 / 失败）。

    为什么不能直接看 ``events[-1].msg.kind``：turn 终态之后还会跟
    ``rewind_checkpoint_recorded`` 这类**记账事件**，它到没到全看调度时序 ——
    按 ``[-1]`` 断言会间歇误判（2026-09-03 实测：挂起族 10 轮红 5 轮，
    失败原文即 ``assert 'rewind_checkpoint_recorded' == 'turn_suspended'``）。

    与 :func:`run_until_root_done` 同源的一条纪律：**断言要挑事件，不要挑位置**。

    Args:
        events: 保序的事件列表（通常来自 ``subscribe_all`` 收集器）。

    Returns:
        最后一条 turn 终态事件的 kind；一条都没有则返回 ``None``。
    """
    terminals = [ev.msg.kind for ev in events if ev.msg.kind in TURN_TERMINAL_KINDS]
    return str(terminals[-1]) if terminals else None
