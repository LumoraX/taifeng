"""并发 fan-out 边界（端到端走 EnginePool）：

- max_parallel>1 时一批 parallel_safe 工具并发执行（断言**峰值并发度**，不看 wall-clock）
- max_parallel=1 退化为串行
- Semaphore 上限分批
均通过自定义带 ``asyncio.sleep`` 的 parallel_safe 工具 + SimClient 脚本驱动。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import GUARD_TIMEOUT_SECONDS, OverlapProbe, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path


def _slow_tool(delay: float, probe: OverlapProbe | None = None) -> ToolSpec:
    """构造一个 sleep `delay` 秒后返回 ok 的 parallel_safe 工具。

    传入 ``probe`` 时顺带记录峰值并发度（供结构性断言用）。
    """

    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        if probe is not None:
            probe.enter()
        try:
            await asyncio.sleep(delay)
        finally:
            if probe is not None:
                probe.exit()
        return ToolResult.ok("slow-done")

    return ToolSpec(
        name="slow_read",
        description="测试用：sleep 后返回（parallel_safe）",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=handler,
        parallel_safe=True,
        timeout_seconds=30.0,
    )


def _client(n: int) -> SimClient:
    """首轮吐 n 个 slow_read 调用，次轮空文本结束。"""
    calls = [
        {"id": f"c{i}", "name": "slow_read", "arguments": "{}"} for i in range(n)
    ]
    return SimClient(turns=[
        SimTurn(text="批量读取", tool_calls=calls),
        SimTurn(text="完成。"),
    ])


_BATCH_READER = """---
name: batch-reader
description: 批量读取入口（声明 slow_read，LLM 才看得到该工具）
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: []
tool_names: [slow_read]
max_call_depth: 2
---
# 批量读取
"""


async def _run_once(
    skills_dir: Path, threads_dir: Path, *, n: int, delay: float, cap: int
) -> int:
    """跑一次 turn，返回工具执行的**峰值并发度**。"""
    # conformance 响应侧反查要求脚本调用的工具必须声明进 skill tool_names——
    # 共享 conftest skill 未声明 slow_read，这里建专用 entry
    (skills_dir / "batch-reader").mkdir(exist_ok=True)
    (skills_dir / "batch-reader" / "SKILL.md").write_text(
        _BATCH_READER, encoding="utf-8"
    )
    probe = OverlapProbe()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=_client(n),
        compressors=[],
        extra_tools=[_slow_tool(delay, probe)],
        max_parallel_tool_calls=cap,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="batch-reader")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break
    await pool.close()
    return probe.peak


@pytest.mark.asyncio
async def test_concurrent_when_cap_gt_one(skills_dir: Path, threads_dir: Path) -> None:
    """3 个调用、cap=4：三者必须真的同时在跑（峰值并发=3）。"""
    peak = await _run_once(skills_dir, threads_dir, n=3, delay=0.3, cap=4)
    assert peak == 3, f"期望三者并发（峰值=3），实测峰值 {peak}"


@pytest.mark.asyncio
async def test_serial_when_cap_one(skills_dir: Path, threads_dir: Path) -> None:
    """3 个调用、cap=1：任何时刻至多一个在跑（峰值并发=1）。"""
    peak = await _run_once(skills_dir, threads_dir, n=3, delay=0.2, cap=1)
    assert peak == 1, f"期望串行（峰值=1），实测峰值 {peak}"


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency(skills_dir: Path, threads_dir: Path) -> None:
    """4 个调用、cap=2：信号量必须把峰值并发恰好压在 2。"""
    peak = await _run_once(skills_dir, threads_dir, n=4, delay=0.2, cap=2)
    assert peak == 2, f"期望信号量封顶 2（峰值=2），实测峰值 {peak}"


# ----------------------------------------------------------------------
# 头条场景：一条消息里的两个 call_skill 并发（用户"两条旅游线路"诉求）
# ----------------------------------------------------------------------

_ENTRY_SKILL = """---
name: planner
description: 行程规划编排
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [route-a, route-b]
tool_names: []
max_call_depth: 3
---
# 规划编排 ENTRY_MARK
同时规划两条线路。
"""

_ROUTE_A = """---
name: route-a
description: 线路甲规划
version: 1.0.0
type: atomic
---
# 线路甲 CHILD_A_MARK
推算线路甲各节点。
"""

_ROUTE_B = """---
name: route-b
description: 线路乙规划
version: 1.0.0
type: atomic
---
# 线路乙 CHILD_B_MARK
推算线路乙各节点。
"""


def _build_two_route_skills(tmp_path: Path) -> Path:
    """内联写出 planner(entry) + route-a + route-b 三个 skill，返回 skills 目录。"""
    skills = tmp_path / "planner_skills"
    for sub, body in (
        ("planner", _ENTRY_SKILL),
        ("route-a", _ROUTE_A),
        ("route-b", _ROUTE_B),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _planner_routing_client() -> RoutingSimClient:
    """RoutingSimClient：entry 首轮吐两个 call_skill；两条子线路各 sleep 0.3s。

    路由 key 取各 skill body 内的唯一标记（entry prompt 不含子 body，子 turn
    prompt 含自身 body → 标记互不串扰）。ENTRY_MARK 放首位优先匹配。
    """
    return RoutingSimClient(routes={
        "ENTRY_MARK": [
            SimTurn(text="并行规划两条线路", tool_calls=[
                {"id": "c0", "name": "call_skill",
                 "arguments": '{"skill_id": "route-a", "reason": "plan A"}'},
                {"id": "c1", "name": "call_skill",
                 "arguments": '{"skill_id": "route-b", "reason": "plan B"}'},
            ]),
            SimTurn(text="两条线路规划完毕。"),
        ],
        "CHILD_A_MARK": [SimTurn(text="线路甲完成", delay_seconds=0.3)],
        "CHILD_B_MARK": [SimTurn(text="线路乙完成", delay_seconds=0.3)],
    })


async def _run_planner(
    skills: Path, threads_dir: Path, *, cap: int
) -> tuple[set[str], int]:
    """跑 planner 编排，返回 (实际 returned 的子 skill 集合, 子线路峰值并发度)。

    用 subscribe_all 监听到【根 turn 完成（is_root=True）】才收尾——不能用
    subscribe(sub_id) 在首个 turn_completed 退出，因为子 turn 也会 emit
    turn_completed（is_root=False），会让观测在第一条子线路完成时就提前结束。
    """
    pool = await taifeng.EnginePool.create(
        skills_dir=skills,
        threads_dir=threads_dir,
        model_client=_planner_routing_client(),
        compressors=[],
        max_parallel_tool_calls=cap,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="planner")

    returned: set[str] = set()
    root_done = asyncio.Event()
    # 峰值并发：已 dispatch 未 return 的子 skill 数的最大值。这是「两条线路是否
    # 真的同时在跑」的**结构性**判据；墙钟只是它的间接投影，机器一慢就失真。
    probe = OverlapProbe()

    async def consume() -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "skill_dispatched":
                probe.enter()
            if ev.msg.kind == "skill_returned":
                probe.exit()
                returned.add(ev.msg.data.get("skill_id", ""))
            if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root"):
                root_done.set()
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(consume())
    # create_task 只排期；确认订阅登记上再提交，否则首批 dispatch 事件会漏
    await wait_for_condition(
        lambda: bool(engine._all_subs),  # noqa: SLF001
        message="firehose 订阅未能登记",
    )
    await engine.submit(taifeng.UserMessage(text="规划两条线路"))
    await asyncio.wait_for(root_done.wait(), timeout=GUARD_TIMEOUT_SECONDS)
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
    except TimeoutError:
        task.cancel()
    return returned, probe.peak


@pytest.mark.asyncio
async def test_two_call_skills_dispatch_concurrently(
    tmp_path: Path, threads_dir: Path
) -> None:
    """两个 call_skill 同批、cap=2：两条子线路必须真的同时在跑（峰值并发=2）。"""
    skills = _build_two_route_skills(tmp_path)
    returned, peak = await _run_planner(skills, threads_dir, cap=2)
    assert returned == {"route-a", "route-b"}, f"实际 returned={returned}"
    assert peak == 2, f"期望两条线路并发（峰值=2），实测峰值 {peak}"


@pytest.mark.asyncio
async def test_two_call_skills_serial_when_cap_one(
    tmp_path: Path, threads_dir: Path
) -> None:
    """同场景 cap=1：两条子线路串行 → 任何时刻至多一条在跑（峰值并发=1，回归对照）。"""
    skills = _build_two_route_skills(tmp_path)
    returned, peak = await _run_planner(skills, threads_dir, cap=1)
    assert returned == {"route-a", "route-b"}, f"实际 returned={returned}"
    assert peak == 1, f"期望串行（峰值=1），实测峰值 {peak}"
