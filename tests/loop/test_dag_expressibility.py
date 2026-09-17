"""验证「内核已能表达任意 DAG」这一断言（skill-orchestration.md §扩展边界与后路）。

该断言是「不做 `depends_on` / 拓扑排序」的立论前提：节点 = `spawn_skill`，边 =
`set_join_barrier(handle_ids, then_skill)`，扇入是 barrier 的天然语义。此前测试只覆盖
**一层**扇出扇入（一个 barrier、then_skill 不再 spawn），断言的两个要害没有用例钉住：

1. 非 series-parallel 形状：「D 只等 A 和 C，不等 B」——文档明说声明式 `steps` 表达不了；
2. 多级：barrier 触发的 skill 能否继续 spawn，以及 barrier 能否链式串联。

本文件把这两点固化为回归。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_spawn_skill_tool,
)

if TYPE_CHECKING:
    from pathlib import Path

_ATOMIC = """---
name: {name}
description: DAG 节点 {name}
version: 1.0.0
type: atomic
---
# {name}
节点 {name} 的工作。
"""

_ROOT = """---
name: dag-root
description: DAG 编排入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [node-a, node-b, node-c, node-d, node-e, level2, final]
tool_names: [spawn_skill, await_skills]
max_call_depth: 3
---
# DAG 编排入口
按运行时句柄接线任意 DAG。
"""

# barrier 触发的聚合 skill，自身再 spawn 孙节点 —— 多级 DAG 的关键一跳
_LEVEL2 = """---
name: level2
description: 二级聚合并继续扇出
version: 1.0.0
type: composite
model: mock-model
child_skills: [node-d, node-e]
tool_names: [spawn_skill]
max_call_depth: 3
---
# 二级聚合
收到一级结论后继续扇出两个孙节点。
"""


@pytest.fixture
def dag_skills(tmp_path: Path) -> Path:
    """建一个 DAG 专用 skills 目录（不复用 conftest 的两 skill 夹具）。"""
    skills = tmp_path / "dag-skills"
    for name in ("node-a", "node-b", "node-c", "node-d", "node-e", "final"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(_ATOMIC.format(name=name), encoding="utf-8")
    (skills / "dag-root").mkdir(parents=True)
    (skills / "dag-root" / "SKILL.md").write_text(_ROOT, encoding="utf-8")
    (skills / "level2").mkdir(parents=True)
    (skills / "level2" / "SKILL.md").write_text(_LEVEL2, encoding="utf-8")
    return skills


async def _wait(cond: Any, timeout_s: float = 5.0) -> bool:
    """轮询等待条件成立（成立返回 True，超时 False）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_fan_in_subset_d_waits_a_and_c_not_b(dag_skills: Path, threads_dir: Path) -> None:
    """「D 只等 A 和 C，不等 B」：barrier 在 B 仍在跑时就该触发。

    这正是 `orchestration.steps` 的 series-parallel 结构表达不了的形状——段间是全量
    barrier，不能让某个后继只等前一段的**子集**。
    """
    client = RoutingSimClient(routes={
        "节点 node-a": [SimTurn(text="A 完成")],
        # B 慢：barrier 触发时它必须还在跑，才能证明 D 没等它
        "节点 node-b": [SimTurn(text="B 完成", delay_seconds=3.0)],
        "节点 node-c": [SimTurn(text="C 完成")],
        "节点 final": [SimTurn(text="D：综合 A 与 C")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=dag_skills, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="dag-subset", entry_skill_id="dag-root")

    fired: dict[str, Any] = {}

    async def watch() -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired.update(ev.msg.data)
                return

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="node-a", args={}, reason="A"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="node-b", args={}, reason="B"))["handle_id"]
    c = (await engine.spawn_skill(skill_id="node-c", args={}, reason="C"))["handle_id"]
    # 边：D ← {A, C}，**不含 B**
    await engine.set_join_barrier([a, c], then_skill_id="final")

    assert await _wait(lambda: bool(fired)), "A/C 都终态后 barrier 未触发"
    # 要害断言：触发瞬间 B 仍未终态 —— 证明扇入的是子集而非全量
    assert engine.spawn_status([b])[b]["status"] == "running", \
        "B 已终态，本用例没能证明「只等子集」（B 的 delay 需大于 A/C 的完成时间）"

    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_multi_level_dag_barrier_fired_skill_spawns_and_chains(
    dag_skills: Path, threads_dir: Path,
) -> None:
    """多级 DAG：barrier 触发的 skill 继续 spawn 孙节点，第二道 barrier 再串一跳。

    另钉一条治理约束：spawn 白名单以 **engine entry** 的 child_skills 为准
    （`spawn_driver.spawn_skill` 以 entry 为唯一栈帧做 DispatchPolicy 裁决），
    因此孙节点也必须声明在 entry 的产品包内——DAG 的**节点集合**由 entry 声明背书，
    只有**边**才是运行时接线。

    钉住两件此前无用例覆盖的事：
    a) `_build_child_runner` 给 barrier 起的聚合 turn 也注入了 spawn_coordinator
       —— 否则聚合 skill 调 spawn_skill 会拿到 `spawn_unavailable`，多级 DAG 直接断链；
    b) 第二道 barrier 可以架在孙句柄上，即 barrier 能链式串联。
    """
    client = RoutingSimClient(routes={
        "节点 node-a": [SimTurn(text="A 完成")],
        "节点 node-c": [SimTurn(text="C 完成")],
        # 一级 barrier 起的聚合 skill：继续扇出两个孙节点
        "二级聚合": [
            SimTurn(tool_calls=[
                {"id": "t1", "name": "spawn_skill",
                 "arguments": '{"skill_id": "node-d", "args": {}, "reason": "扇出 D"}'},
                {"id": "t2", "name": "spawn_skill",
                 "arguments": '{"skill_id": "node-e", "args": {}, "reason": "扇出 E"}'},
            ]),
            SimTurn(text="已扇出孙节点"),
        ],
        "节点 node-d": [SimTurn(text="D 完成")],
        "节点 node-e": [SimTurn(text="E 完成")],
        "节点 final": [SimTurn(text="最终汇总")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=dag_skills, threads_dir=threads_dir, model_client=client, compressors=[],
        # spawn 工具是业务侧注入项（内核不自动注册）——DAG 由 LLM 接线时必须给
        extra_tools=[make_spawn_skill_tool(), make_await_skills_tool()])
    engine = await pool.get_or_create(session_id="dag-multi", entry_skill_id="dag-root")

    spawned: dict[str, str] = {}      # skill_id -> handle_id
    fired: list[str] = []
    rejected: list[dict[str, Any]] = []

    async def watch() -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_started":
                spawned[ev.msg.data["skill_id"]] = ev.msg.data["handle_id"]
            elif ev.msg.kind == "join_barrier_fired":
                fired.append(ev.msg.data["barrier_id"])
            elif ev.msg.kind == "skill_spawn_rejected":
                rejected.append(dict(ev.msg.data))

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="node-a", args={}, reason="A"))["handle_id"]
    c = (await engine.spawn_skill(skill_id="node-c", args={}, reason="C"))["handle_id"]
    await engine.set_join_barrier([a, c], then_skill_id="level2")

    # a) 聚合 skill 真的 spawn 出了孙节点
    assert await _wait(lambda: "node-d" in spawned and "node-e" in spawned), \
        f"barrier 起的聚合 turn 未能 spawn 孙节点；rejected={rejected}"
    d, e = spawned["node-d"], spawned["node-e"]

    # b) 第二道 barrier 架在孙句柄上 → 再串一跳
    await engine.set_join_barrier([d, e], then_skill_id="final")
    assert await _wait(lambda: len(fired) >= 2), f"第二道 barrier 未触发（fired={fired}）"

    assert not rejected, f"不应有 spawn 被拒：{rejected}"
    task.cancel()
    await pool.close()
