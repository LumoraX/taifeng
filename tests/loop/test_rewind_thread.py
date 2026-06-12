"""thread-addressable rewind —— Rewind Op 的 spawn 子 thread 寻址测试。

契约:openspec/changes/thread-addressable-rewind/specs/thread-addressable-rewind/spec.md
设计:同 change design.md(D1–D7)。

覆盖面:
- Rewind.thread_id 字段(缺省 None 向后兼容)
- rewind_nodes_for 只读入口(根 tid 等价内存表;子 tid raw → reconstruct → derive)
- 活性守卫(unknown_thread / thread_running / turn_suspended / unknown_node;
  禁状态白名单 —— error 终态与 stale running 均放行)
- 截断重推(error → re_reason 重推落 done / retry_tool 换参 / 再失败可再 rewind /
  冷恢复坐标自洽 / kill_spawn 可取消重推)
"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.submission import Rewind
from taifeng.tool.spec import ToolResult, ToolSpec

_WORKER = """---
name: worker
description: 可派发 echo 的工作者
version: 1.0.0
type: composite
model: mock-model
tool_names: [echo]
max_call_depth: 2
---
# 工作者 WORKER_MARK
按需调用 echo。
"""

_HOST = """---
name: host
description: 宿主入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker]
max_call_depth: 3
---
# 宿主 HOST_MARK
派发工作者。
"""


@pytest.fixture
def rw_skills(tmp_path):
    """host(entry) + worker(被 spawn 的目标,声明 echo 工具)。"""
    skills = tmp_path / "rw_skills"
    for sub, body in (("host", _HOST), ("worker", _WORKER)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待条件成立(每次 10ms,默认最多 3s),等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _echo_tool(calls: list[dict] | None = None) -> ToolSpec:
    """极简 echo 工具;传入 calls 列表时侧录每次实参(retry_tool 换参断言用)。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True,
    )


def _tool_turn(i: int, args: str = "{}") -> SimTurn:
    """带一次 echo 派发的采样剧本(产生 dispatch 节点)。"""
    return SimTurn(text=f"第{i}轮派发。", tool_calls=[
        {"id": f"c{i}", "name": "echo", "arguments": args}])


async def _make_pool(skills_dir, threads_dir, routes, **kwargs):
    """统一建池:RoutingSimClient + echo 工具,缺省 max_iterations=1(便于造 error 终态)。"""
    client = RoutingSimClient(routes=routes)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[kwargs.pop("echo", None) or _echo_tool()],
        max_iterations=kwargs.pop("max_iterations", 1),
        **kwargs,
    )
    return pool, client


async def _spawn_until_error(engine) -> tuple[str, str]:
    """spawn worker 至 error 终态(max_iterations=1 + 一次派发即触顶)。"""
    out = await engine.spawn_skill(skill_id="worker", args={}, reason="t")
    hid, ctid = out["handle_id"], out["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "error"
    ), "max_iterations=1 + 一次派发应令 spawn 落 error 终态"
    return hid, ctid


async def _collect_rejected(engine, sub_id: str) -> dict:
    """消费事件直到该 submission 的 rewind_rejected,返回其 data。"""
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        if ev.msg.kind == "rewind_rejected":
            return dict(ev.msg.data)
    raise AssertionError("未收到 rewind_rejected")


# ──────────────────────────────────────────────────────────────────────
# T1.1 Rewind Op:thread_id 字段
# ──────────────────────────────────────────────────────────────────────


def test_rewind_op_thread_id_defaults_none() -> None:
    """缺省 thread_id 为 None(向后兼容:既有调用零改动)。"""
    op = Rewind(node_id="t1:it1")
    assert op.thread_id is None


def test_rewind_op_thread_id_roundtrip() -> None:
    """显式 thread_id 可设置并经 model_dump 往返。"""
    op = Rewind(node_id="t1:disp0", thread_id="thr-child-1", mode="re_reason")
    assert op.thread_id == "thr-child-1"
    assert Rewind(**op.model_dump()).thread_id == "thr-child-1"


# ──────────────────────────────────────────────────────────────────────
# T1.2 rewind_nodes_for:按 thread_id 查节点表
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_nodes_for_root_equals_property(
    skills_dir, threads_dir
) -> None:
    """根 thread_id 查询等价既有 rewind_nodes() 内存表。"""
    pool, _ = await _make_pool(skills_dir, threads_dir, routes={
        "代码审查专家": [SimTurn(text="根回复")],
    }, max_iterations=3)
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind == "turn_completed":
            break
    assert await _wait(lambda: bool(engine.rewind_nodes()))
    nodes = await engine.rewind_nodes_for(engine.thread_id)
    assert [c.node_id for c in nodes] == [
        c.node_id for c in engine.rewind_nodes()]
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_nodes_for_failed_spawn_has_dispatch(
    rw_skills, threads_dir
) -> None:
    """error 终态的 spawn 子 thread 节点表含失败派发的 dispatch 节点。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "WORKER_MARK": [_tool_turn(1)],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="host")
    _hid, ctid = await _spawn_until_error(engine)
    nodes = await engine.rewind_nodes_for(ctid)
    disp = [c for c in nodes if c.kind == "dispatch"]
    assert disp, f"子 thread 节点表应含 dispatch 节点,实得 {nodes}"
    assert disp[0].target_id == "echo"
    # iteration 节点同样可寻址
    assert any(c.kind == "iteration" for c in nodes)
    await pool.close()
