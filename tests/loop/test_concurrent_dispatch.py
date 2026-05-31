"""并发 fan-out 边界（端到端走 EnginePool）：

- max_parallel>1 时一批 parallel_safe 工具并发执行（wall-clock 明显 < 串行和）
- max_parallel=1 退化为串行
- Semaphore 上限分批
均通过自定义带 ``asyncio.sleep`` 的 parallel_safe 工具 + MockClient 脚本驱动。
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.providers.mock import RoutingMockClient
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path


def _slow_tool(delay: float) -> ToolSpec:
    """构造一个 sleep `delay` 秒后返回 ok 的 parallel_safe 工具。"""

    async def handler(args: dict, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(delay)
        return ToolResult.ok("slow-done")

    return ToolSpec(
        name="slow_read",
        description="测试用：sleep 后返回（parallel_safe）",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=handler,
        parallel_safe=True,
        timeout_seconds=30.0,
    )


def _client(n: int) -> MockClient:
    """首轮吐 n 个 slow_read 调用，次轮空文本结束。"""
    calls = [
        {"id": f"c{i}", "name": "slow_read", "arguments": "{}"} for i in range(n)
    ]
    return MockClient(turns=[
        MockTurn(text="批量读取", tool_calls=calls),
        MockTurn(text="完成。"),
    ])


async def _run_once(
    skills_dir: Path, threads_dir: Path, *, n: int, delay: float, cap: int
) -> float:
    """跑一次 turn，返回 wall-clock 秒。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=_client(n),
        compressors=[],
        extra_tools=[_slow_tool(delay)],
        max_parallel_tool_calls=cap,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    start = time.monotonic()
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break
    elapsed = time.monotonic() - start
    await pool.close()
    return elapsed


@pytest.mark.asyncio
async def test_concurrent_when_cap_gt_one(skills_dir: Path, threads_dir: Path) -> None:
    """3 个 sleep 0.3s、cap=4：wall-clock 应明显 < 0.6s（并发）。"""
    elapsed = await _run_once(skills_dir, threads_dir, n=3, delay=0.3, cap=4)
    assert elapsed < 0.5, f"期望并发(<0.5s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_serial_when_cap_one(skills_dir: Path, threads_dir: Path) -> None:
    """3 个 sleep 0.2s、cap=1：wall-clock 应 >= 0.55s（串行）。"""
    elapsed = await _run_once(skills_dir, threads_dir, n=3, delay=0.2, cap=1)
    assert elapsed >= 0.55, f"期望串行(>=0.55s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency(skills_dir: Path, threads_dir: Path) -> None:
    """4 个 sleep 0.2s、cap=2：约两批 → >=0.4s 且 < 0.75s。"""
    elapsed = await _run_once(skills_dir, threads_dir, n=4, delay=0.2, cap=2)
    assert 0.4 <= elapsed < 0.75, f"期望两批({elapsed:.2f}s)"


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


def _planner_routing_client() -> RoutingMockClient:
    """RoutingMockClient：entry 首轮吐两个 call_skill；两条子线路各 sleep 0.3s。

    路由 key 取各 skill body 内的唯一标记（entry prompt 不含子 body，子 turn
    prompt 含自身 body → 标记互不串扰）。ENTRY_MARK 放首位优先匹配。
    """
    return RoutingMockClient(routes={
        "ENTRY_MARK": [
            MockTurn(text="并行规划两条线路", tool_calls=[
                {"id": "c0", "name": "call_skill",
                 "arguments": '{"skill_id": "route-a", "reason": "plan A"}'},
                {"id": "c1", "name": "call_skill",
                 "arguments": '{"skill_id": "route-b", "reason": "plan B"}'},
            ]),
            MockTurn(text="两条线路规划完毕。"),
        ],
        "CHILD_A_MARK": [MockTurn(text="线路甲完成", delay_seconds=0.3)],
        "CHILD_B_MARK": [MockTurn(text="线路乙完成", delay_seconds=0.3)],
    })


async def _run_planner(skills: Path, threads_dir: Path, *, cap: int) -> tuple[float, set[str]]:
    """跑 planner 编排，返回 (根 turn wall-clock 秒, 实际 returned 的子 skill 集合)。

    用 subscribe_all 监听到【根 turn 完成（is_root=True）】才计时收尾——不能用
    subscribe(sub_id) 在首个 turn_completed 退出，因为子 turn 也会 emit
    turn_completed（is_root=False），会让测量在第一条子线路完成时就提前结束。
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
    elapsed_holder: dict[str, float] = {}

    async def consume() -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "skill_returned":
                returned.add(ev.msg.data.get("skill_id", ""))
            if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root"):
                elapsed_holder["t"] = time.monotonic() - start
                root_done.set()
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(consume())
    start = time.monotonic()
    await engine.submit(taifeng.UserMessage(text="规划两条线路"))
    await asyncio.wait_for(root_done.wait(), timeout=3.0)
    await pool.close()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
    return elapsed_holder["t"], returned


@pytest.mark.asyncio
async def test_two_call_skills_dispatch_concurrently(
    tmp_path: Path, threads_dir: Path
) -> None:
    """两个 call_skill 同批、cap=2：两条子线路并发 → 根 turn wall-clock < 0.5s（各 0.3s）。"""
    skills = _build_two_route_skills(tmp_path)
    elapsed, returned = await _run_planner(skills, threads_dir, cap=2)
    assert returned == {"route-a", "route-b"}, f"实际 returned={returned}"
    # 并发：两条各 0.3s，若串行需 >=0.6s；并发应 < 0.5s
    assert elapsed < 0.5, f"期望两条线路并发(<0.5s)，实测 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_two_call_skills_serial_when_cap_one(
    tmp_path: Path, threads_dir: Path
) -> None:
    """同场景 cap=1：两条子线路串行 → 根 turn wall-clock >= 0.55s（回归对照）。"""
    skills = _build_two_route_skills(tmp_path)
    elapsed, returned = await _run_planner(skills, threads_dir, cap=1)
    assert returned == {"route-a", "route-b"}, f"实际 returned={returned}"
    assert elapsed >= 0.55, f"期望串行(>=0.55s)，实测 {elapsed:.2f}s"
