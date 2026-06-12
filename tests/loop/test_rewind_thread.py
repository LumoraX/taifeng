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


# ──────────────────────────────────────────────────────────────────────
# T2 活性守卫:unknown_thread / thread_running / turn_suspended / unknown_node
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_unknown_thread_rejected(rw_skills, threads_dir) -> None:
    """thread_id 不属于任何 spawn 句柄 → rewind_rejected(unknown_thread)。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="g1", entry_skill_id="host")
    sub_id = await engine.submit(Rewind(node_id="t1:it1", thread_id="no-such"))
    data = await asyncio.wait_for(_collect_rejected(engine, sub_id), timeout=3)
    assert data["reason"] == "unknown_thread"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_running_spawn_rejected(rw_skills, threads_dir) -> None:
    """子 thread 热跑中(live runner)→ rewind_rejected(thread_running)。"""
    pool, client = await _make_pool(rw_skills, threads_dir, routes={
        # 子 turn 卡在 await_signal,保持 live 状态直到测试放行
        "WORKER_MARK": [SimTurn(text="慢", await_signal="release")],
        "HOST_MARK": [SimTurn(text="主")],
    }, max_iterations=3)
    engine = await pool.get_or_create(session_id="g2", entry_skill_id="host")
    out = await engine.spawn_skill(skill_id="worker", args={}, reason="t")
    hid, ctid = out["handle_id"], out["child_thread_id"]
    try:
        # spawn 后立即 rewind:子 runner 还卡在采样信号上(running + live)
        sub_id = await engine.submit(Rewind(node_id="t1:it1", thread_id=ctid))
        data = await asyncio.wait_for(
            _collect_rejected(engine, sub_id), timeout=3)
        assert data["reason"] == "thread_running"
    finally:
        client.coordinator.signal("release")
        await _wait(
            lambda: engine.spawn_status([hid])[hid]["status"] == "done")
        await pool.close()


@pytest.mark.asyncio
async def test_rewind_suspended_spawn_rejected(rw_skills, threads_dir) -> None:
    """子 thread 活跃挂起 → rewind_rejected(turn_suspended),挂起走 Resume。"""
    from taifeng import SuspendByDefaultPolicy

    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "WORKER_MARK": [_tool_turn(1)],
        "HOST_MARK": [SimTurn(text="主")],
    }, failure_policy=SuspendByDefaultPolicy())
    engine = await pool.get_or_create(session_id="g3", entry_skill_id="host")
    out = await engine.spawn_skill(skill_id="worker", args={}, reason="t")
    hid, ctid = out["handle_id"], out["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"
    ), "SuspendByDefaultPolicy 下触顶应挂起"
    sub_id = await engine.submit(Rewind(node_id="t1:it1", thread_id=ctid))
    data = await asyncio.wait_for(_collect_rejected(engine, sub_id), timeout=3)
    assert data["reason"] == "turn_suspended"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_unknown_node_on_child_rejected(
    rw_skills, threads_dir
) -> None:
    """error 终态子 thread + 不存在的 node_id → rewind_rejected(unknown_node)。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "WORKER_MARK": [_tool_turn(1)],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="g4", entry_skill_id="host")
    _hid, ctid = await _spawn_until_error(engine)
    sub_id = await engine.submit(
        Rewind(node_id="does-not-exist", thread_id=ctid))
    data = await asyncio.wait_for(_collect_rejected(engine, sub_id), timeout=3)
    assert data["reason"] == "unknown_node"
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


# ──────────────────────────────────────────────────────────────────────
# T3 截断与重推:re_reason / retry_tool / 再失败 / 冷恢复 / kill / barrier
# ──────────────────────────────────────────────────────────────────────


async def _rewound_data(engine, sub_id: str) -> dict:
    """消费事件直到该 submission 的 turn_rewound,返回其 data。"""
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        if ev.msg.kind == "turn_rewound":
            return dict(ev.msg.data)
        if ev.msg.kind == "rewind_rejected":
            raise AssertionError(f"意外被拒: {ev.msg.data}")
    raise AssertionError("未收到 turn_rewound")


@pytest.mark.asyncio
async def test_rewind_failed_spawn_re_reason_to_done(
    rw_skills, threads_dir
) -> None:
    """核心业务诉求:error 终态 → dispatch 节点 re_reason 重推 → 落 done。

    重推剧本只回文本(LLM 重新决策不再派发),1 圈内完成 → SpawnCompleted。
    TurnRewound 带 thread_id(R3)。
    """
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        # 脚本 1:派发 echo(触顶 error);脚本 2:重推后纯文本完成
        "WORKER_MARK": [_tool_turn(1), SimTurn(text="重跑成功")],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="r1", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    nodes = await engine.rewind_nodes_for(ctid)
    disp = next(c for c in nodes if c.kind == "dispatch")
    sub_id = await engine.submit(
        Rewind(node_id=disp.node_id, thread_id=ctid, mode="re_reason"))
    data = await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    assert data["thread_id"] == ctid
    assert data["node_id"] == disp.node_id
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    ), "re_reason 重推应令句柄落 done"
    assert engine.spawn_status([hid])[hid]["result"] == "重跑成功"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_retry_tool_new_args_store_untouched(
    rw_skills, threads_dir
) -> None:
    """retry_tool + new_args:换参重跑工具,store 原 fc 原样(append-only)。"""
    calls: list[dict] = []
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        # 脚本 1:原参派发(error);脚本 2:重推续采样(seed 补跑后)纯文本完成
        "WORKER_MARK": [_tool_turn(1, args='{"v": "old"}'),
                        SimTurn(text="换参完成")],
        "HOST_MARK": [SimTurn(text="主")],
    }, echo=_echo_tool(calls))
    engine = await pool.get_or_create(session_id="r2", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    assert calls and calls[0] == {"v": "old"}
    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id = await engine.submit(Rewind(
        node_id=disp.node_id, thread_id=ctid,
        mode="retry_tool", new_args={"v": "new"}))
    await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    # 工具以新参重跑(seed 补跑)
    assert calls[-1] == {"v": "new"}, f"echo 应收到 new_args,实得 {calls}"
    # store 原 fc 原样保留(append-only;改写只在内存 buffer)
    raw = [it async for it in await pool.store.load_thread(ctid)]
    fc = [it for it in raw if it.kind == "function_call"]
    assert '"old"' in fc[0].payload["arguments"]
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_fail_again_then_rewind_again(
    rw_skills, threads_dir
) -> None:
    """重推再失败 → 句柄回 error 可再 rewind;二次 marker 叠加坐标自洽。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        # 脚本 1:派发(error);脚本 2:重推又派发(再 error);脚本 3:成功
        "WORKER_MARK": [_tool_turn(1), _tool_turn(2), SimTurn(text="三跑成功")],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="r3", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id = await engine.submit(
        Rewind(node_id=disp.node_id, thread_id=ctid))
    await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "error"
    ), "重推剧本再次派发应再次触顶落 error"
    # 二次 rewind:节点表在已有 marker 上重新推导(坐标自洽)
    disp2 = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id2 = await engine.submit(
        Rewind(node_id=disp2.node_id, thread_id=ctid))
    await asyncio.wait_for(_rewound_data(engine, sub_id2), timeout=3)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    assert engine.spawn_status([hid])[hid]["result"] == "三跑成功"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_stale_running_allowed(rw_skills, threads_dir) -> None:
    """中断遗留 running(无 live runner)放行 —— 锁死「禁状态白名单」。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "WORKER_MARK": [_tool_turn(1), SimTurn(text="补跑成功")],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="r4", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    # 模拟冷重建推断遗留态:句柄被标 running 但无 live runner(白盒;
    # _infer_spawn_status_from_child 对中断子 thread 正是保持 running)
    engine._spawn_handles.set_result(hid, status="running", result=None)  # noqa: SLF001
    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id = await engine.submit(Rewind(node_id=disp.node_id, thread_id=ctid))
    await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_marker_cold_reconstruct_consistent(
    rw_skills, threads_dir
) -> None:
    """marker 落盘后冷加载:reconstruct 截断结果与热路径一致(R5)。"""
    from taifeng.conversation.reconstruct import reconstruct_logical_history

    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        "WORKER_MARK": [_tool_turn(1), SimTurn(text="重跑成功")],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="r5", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id = await engine.submit(Rewind(node_id=disp.node_id, thread_id=ctid))
    data = await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    await _wait(lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    # 冷重放:raw 含 marker;reconstruct 后被截掉的旧 echo 派发不复现
    raw = [it async for it in await pool.store.load_thread(ctid)]
    markers = [
        it for it in raw
        if it.kind == "system_injection"
        and it.payload.get("source") == "rewind"]
    assert markers and markers[0].payload["cut_index"] == data["cut_index"]
    logical = reconstruct_logical_history(raw)
    # 截断点之前的项保留(seed user_message),旧派发圈被折叠掉
    fc_in_logical = [it for it in logical if it.kind == "function_call"]
    assert not fc_in_logical, (
        "re_reason 截到采样前,重放后的逻辑 history 不应再含旧 function_call")
    assert any(
        it.kind == "assistant_message" and it.payload.get("text") == "重跑成功"
        for it in logical)
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_repush_kill_spawn_cancels(
    rw_skills, threads_dir
) -> None:
    """重推期间 kill_spawn:取消当前重推子树,句柄落 cancelled(R4)。"""
    pool, client = await _make_pool(rw_skills, threads_dir, routes={
        # 脚本 1:派发(error);脚本 2:重推卡在信号上(留出 kill 窗口)
        "WORKER_MARK": [_tool_turn(1),
                        SimTurn(text="慢", await_signal="rw-hold")],
        "HOST_MARK": [SimTurn(text="主")],
    })
    engine = await pool.get_or_create(session_id="r6", entry_skill_id="host")
    hid, ctid = await _spawn_until_error(engine)
    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    sub_id = await engine.submit(Rewind(node_id=disp.node_id, thread_id=ctid))
    await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    # 等重推 runner 上线(live)后 kill;取消是协作式的(事件边界检查),
    # 放行信号让流恢复出事件,runner 在下一边界观察到取消 → cancelled
    assert await _wait(lambda: ctid in engine._spawn._live_runners)  # noqa: SLF001
    await engine.kill_spawn(hid)
    client.coordinator.signal("rw-hold")
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "cancelled"
    ), "kill 重推中的 spawn 应落 cancelled 终态"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_done_spawn_fired_barrier_not_refired(
    rw_skills, threads_dir
) -> None:
    """rewind 已 done 且 barrier 已 fired 的 spawn:重推得新结果,聚合不二次触发。"""
    pool, _ = await _make_pool(rw_skills, threads_dir, routes={
        # 子:首跑文本完成(done)→ barrier fire;rewind 重推再完成
        "WORKER_MARK": [SimTurn(text="一跑"), SimTurn(text="二跑")],
        # host 路由:根 turn(spawn 发起)+ barrier 聚合 turn 各一份剧本
        "HOST_MARK": [SimTurn(text="主"), SimTurn(text="聚合完成")],
    }, max_iterations=3)
    engine = await pool.get_or_create(session_id="r7", entry_skill_id="host")
    fired: list[str] = []

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired.append(ev.msg.data["barrier_id"])
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    out = await engine.spawn_skill(skill_id="worker", args={}, reason="t")
    hid, ctid = out["handle_id"], out["child_thread_id"]
    await engine.set_join_barrier([hid], then_skill_id="host")
    assert await _wait(lambda: len(fired) == 1), "全终态应触发 barrier"
    bid = fired[0]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    # rewind 该 done 句柄(iteration 节点 re_reason)→ 重推得新结果
    it_node = next(
        c for c in await engine.rewind_nodes_for(ctid)
        if c.kind == "iteration")
    sub_id = await engine.submit(Rewind(node_id=it_node.node_id, thread_id=ctid))
    await asyncio.wait_for(_rewound_data(engine, sub_id), timeout=3)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["result"] == "二跑")
    # barrier 幂等:重推终态触发的 _check_barriers 被 fired 守卫跳过
    assert fired.count(bid) == 1, f"已 fired 的 barrier 不得二次触发: {fired}"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
