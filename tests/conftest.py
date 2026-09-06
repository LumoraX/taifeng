"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# ---------------------------------------------------------------------------
# 墙钟守卫期限（防挂死用，**不是**被测行为）
# ---------------------------------------------------------------------------

GUARD_TIMEOUT_SECONDS = float(os.environ.get("TAIFENG_TEST_GUARD_TIMEOUT", "30"))
"""等待「本就该发生的事」时的兜底期限（秒）。

**条件一满足就立即返回**，这个值只在真挂死时才被用到——所以必须**给足**。

历史教训：这里过去按「正常应该多快」估成 1~5 秒，结果全量跑（2300+ 用例、单进程
累积线程 / 事件循环 / GC 压力，整轮 50~80s）时机器一慢就集体误报。2026-09 期间已
观测到 **8 个不同用例**轮流因此变红，其中一次直接把 main 的 CI 跑红，且每次红的
用例都不一样——典型的「守卫期限当成了性能断言」。

判据：**守卫期限只负责「别无限挂」，不负责「多快算对」。** 要断言时序快慢的用例
必须自己写显式期限并注明理由，不得复用本常量。慢环境可用环境变量
``TAIFENG_TEST_GUARD_TIMEOUT`` 整体放大。
"""


async def wait_for_condition(
    predicate: Callable[[], bool],
    *,
    deadline_seconds: float | None = None,
    poll_seconds: float = 0.005,
    message: str = "条件未在守卫期限内满足",
) -> None:
    """轮询等待条件成立——取代「sleep 一个估计值，然后假设它已经发生」。

    ``await asyncio.sleep(0.05)`` 这种写法把「前置条件是否成立」押在机器速度上：
    负载一高睡醒时状态早已越过，用例的前提凭空消失，症状却表现为下游 `wait_for`
    超时，极难归因。改成等真实状态。

    Args:
        predicate: 同步谓词，返回 True 即结束等待。
        deadline_seconds: 守卫期限；None → ``GUARD_TIMEOUT_SECONDS``。
        poll_seconds: 轮询间隔。
        message: 超时断言信息。

    Raises:
        AssertionError: 期限内条件始终不成立。
    """
    budget = GUARD_TIMEOUT_SECONDS if deadline_seconds is None else deadline_seconds
    deadline = time.monotonic() + budget
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(poll_seconds)


@dataclass
class OverlapProbe:
    """记录并发执行的**峰值并发度**——并发/串行的结构性判据，不受机器快慢影响。

    「并发发生了」的直接证据是「同时有几个 handler 在执行」，墙钟耗时只是它的
    间接投影。用 ``elapsed < 0.5`` 这类上界断言等于把「并发」和「快」划等号：
    单进程测试套里 IO / CPU 一争抢，调度延迟就把上界撑破，用例红得跟并发语义
    毫无关系（实测 IO 风暴下 main 必现）。

    而且墙钟断言**比结构断言弱**：信号量若从 cap=2 悄悄退化成 cap=4，只要机器
    够快 ``elapsed`` 照样落在区间内——真回归漏抓；``peak == 2`` 必抓。

    用法::

        probe = OverlapProbe()
        async def handler(...):
            probe.enter()
            try:
                await asyncio.sleep(delay)
            finally:
                probe.exit()
        ...
        assert probe.peak == 2   # 而不是 assert elapsed < 0.5
    """

    active: int = 0
    peak: int = 0

    def enter(self) -> None:
        """进入临界区，刷新峰值。"""
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        """离开临界区。"""
        self.active -= 1


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
