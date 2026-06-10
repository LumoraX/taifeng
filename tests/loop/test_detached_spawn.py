"""分离式 skill spawn + join-barrier 测试。设计见
docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.spawn_handle import SpawnHandleRegistry


async def _wait(cond, tries: int = 200) -> bool:
    """轮询等待条件成立(每次 10ms,默认最多 2s),用于等后台分离 task 收敛。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def test_registry_register_and_lookup() -> None:
    reg = SpawnHandleRegistry()
    h = reg.register(
        handle_id="sp0", skill_id="analyzer", child_thread_id="t-1"
    )
    assert h.status == "running"
    assert reg.get("sp0") is h
    assert reg.get("nope") is None


def test_registry_set_status_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="sp0", skill_id="a", child_thread_id="t-1")
    reg.set_result("sp0", status="done", result="结论A")
    h = reg.get("sp0")
    assert h.status == "done" and h.result == "结论A"
    assert reg.is_terminal("sp0")


def test_registry_all_terminal() -> None:
    reg = SpawnHandleRegistry()
    reg.register(handle_id="a", skill_id="x", child_thread_id="t1")
    reg.register(handle_id="b", skill_id="x", child_thread_id="t2")
    reg.set_result("a", status="done", result="ra")
    assert not reg.all_terminal(["a", "b"])
    reg.set_result("b", status="error", result="boom")
    assert reg.all_terminal(["a", "b"])  # done + error 都算终态


def test_spawn_event_kinds() -> None:
    from taifeng.loop.event import (
        JoinBarrierFired,
        JoinBarrierRegistered,
        SpawnCancelled,
        SpawnCompleted,
        SpawnFailed,
        SpawnStarted,
        SpawnSuspended,
    )
    assert SpawnStarted().kind == "spawn_started"
    assert SpawnSuspended().kind == "spawn_suspended"
    assert SpawnCompleted().kind == "spawn_completed"
    assert SpawnFailed().kind == "spawn_failed"
    assert SpawnCancelled().kind == "spawn_cancelled"
    assert JoinBarrierRegistered().kind == "join_barrier_registered"
    assert JoinBarrierFired().kind == "join_barrier_fired"


def test_spawn_response_items() -> None:
    from taifeng.conversation.models import (
        join_barrier_fired_item,
        join_barrier_item,
        spawn_item,
    )
    si = spawn_item(
        handle_id="sp0", skill_id="a",
        child_thread_id="t1", thread_id="root",
    )
    assert si.kind == "spawn" and si.payload["handle_id"] == "sp0"
    bi = join_barrier_item(
        barrier_id="b0", handle_ids=["sp0"],
        then_skill_id="merge", then_args_template=None,
        thread_id="root",
    )
    assert bi.kind == "join_barrier" and bi.payload["barrier_id"] == "b0"
    fi = join_barrier_fired_item(
        barrier_id="b0", then_thread_id="t9", thread_id="root"
    )
    assert fi.kind == "join_barrier_fired"


@pytest.mark.asyncio
async def test_spawn_returns_handle_nonblocking(
    skills_dir, threads_dir
) -> None:
    """engine.spawn_skill 立即返回句柄(非阻塞),后台分离 task 跑完后句柄转 done。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="风格结论")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer")
    out = await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="并发分析")
    assert out["handle_id"] and out["child_thread_id"]
    # 句柄立即可见(running),后台 task 跑完后转 done
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )
    assert engine.spawn_status([hid])[hid]["result"] == "风格结论"
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_seed_id_consistent_store_vs_memory(
    skills_dir, threads_dir
) -> None:
    """C1：store 里落盘的种子 id 与 history_buffer[0] 使用的 id 一致。

    修复前：_drive_spawn 会重建 seed（新随机 id），导致 store 里 id ≠
    内存中 id，冷恢复时会重建出不同的消息图谱。
    修复后：seed 在 spawn_skill 构造一次，直接传入 _drive_spawn /
    _build_child_runner，两路径共享同一对象，id 天然一致。
    此测试验证子 thread 的首条记录是 user_message 且内容与 args 对应。
    """
    import json

    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="风格结论2")],
        "code-reviewer": [MockTurn(text="主2")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s-c1", entry_skill_id="code-reviewer")

    args_payload = {"key": "value"}
    out = await engine.spawn_skill(
        skill_id="style-checker", args=args_payload, reason="c1-test")

    # 等子 task 完成确保 store 已 flush
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )

    # 从 store 读回 child thread，首条必须是 user_message 且内容与 args 一致
    child_tid = out["child_thread_id"]
    thread_iter = await pool.store.load_thread(child_tid)
    items = [item async for item in thread_iter]
    assert items, "子 thread 至少有一条记录"
    first = items[0]
    assert first.kind == "user_message", (
        f"首条应为 user_message，实际: {first.kind}"
    )
    seed_text = first.payload["text"]
    assert json.loads(seed_text) == args_payload, (
        f"种子内容不匹配: {seed_text}"
    )
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_k1_slot_released_on_create_thread_failure(
    skills_dir, threads_dir
) -> None:
    """C2：create_thread 失败时 K1 spawn 槽位必须释放，不永久泄漏。

    修复前：reserve_manual 与 create_task 之间若抛出，_drive_spawn 从不
    启动，其 finally 中的 release_manual 永远不执行 → 槽位泄漏。
    修复后：spawn_skill 在 try/except 里捕获预启动失败，立即
    release_manual 后重抛。
    """
    from unittest.mock import AsyncMock, patch

    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="ok")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="s-c2", entry_skill_id="code-reviewer")

    # 注入：让 store.create_thread 抛出，模拟预启动失败
    with patch.object(
        engine._store, "create_thread",
        new_callable=AsyncMock,
        side_effect=RuntimeError("模拟 create_thread 失败"),
    ), pytest.raises(RuntimeError, match="模拟 create_thread 失败"):
        await engine.spawn_skill(
            skill_id="style-checker", args={}, reason="c2-test")

    # 槽位必须已被释放（_active 回到 0）
    snap = engine._spawn_registry.snapshot()
    assert snap["active"] == 0, f"K1 槽位泄漏，active={snap['active']}"

    # 泄漏修复后，后续正常 spawn 依然可以成功
    out = await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="c2-後続")
    hid = out["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"
    )
    await pool.close()


@pytest.mark.asyncio
async def test_same_skill_multiple_instances(skills_dir, threads_dir):
    """同一 skill 同时 spawn 三个实例，各自拥有独立句柄和独立 child thread。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="路线1"), MockTurn(text="路线2"), MockTurn(text="路线3")],
        "code-reviewer": [MockTurn(text="主")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    handles = [
        (await engine.spawn_skill(
            skill_id="style-checker", args={"i": i}, reason="路线"))["handle_id"]
        for i in range(3)
    ]
    assert len(set(handles)) == 3  # 三个独立句柄
    assert await _wait(lambda: all(
        engine.spawn_status([h])[h]["status"] == "done" for h in handles))
    # 三条独立 child thread
    threads = {engine._spawn_handles.get(h).child_thread_id for h in handles}  # noqa: SLF001
    assert len(threads) == 3
    await pool.close()


@pytest.mark.asyncio
async def test_spawn_independent_completion_events(skills_dir, threads_dir):
    """两个独立 spawn 各自发出独立的 spawn_completed 事件，handle_id 互不混淆。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A结论"), MockTurn(text="B结论")],
        "code-reviewer": [MockTurn(text="主")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s2b", entry_skill_id="code-reviewer")
    completed: dict[str, str] = {}

    async def watch():
        """订阅所有事件，收集 spawn_completed 直到收到两条为止。"""
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_completed":
                completed[ev.msg.data["handle_id"]] = ev.msg.data.get("result", "")
                if len(completed) >= 2:
                    return

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="a"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="b"))["handle_id"]
    assert await _wait(lambda: len(completed) >= 2)
    assert set(completed.keys()) == {a, b}  # 各自独立 spawn_completed 事件
    task.cancel()
    await pool.close()


# ---------------------------------------------------------------------------
# 错峰独立 HITL：detached 专家在自己的 child thread 上挂起 → 独立 Resume → 完成。
# 两个专家先后各自挂起/恢复，完全解耦（A 先跑完，B 再挂起恢复）。
# ---------------------------------------------------------------------------

# 专家 skill：composite + tool-only（仅 request_user_input），非 entry —— spawn 派发
# 要求 target.entry == false（见 DispatchPolicy.check 第 5 条），故专家不可为 entry。
# orchestrator 才是 entry，把两个专家列进 child_skills 白名单。
_EXPERT_A_SKILL = """---
name: expert-a
description: 专家A
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 专家A EXPERT_A_MARK
你会先用 request_user_input 询问，再据答复给结论。
"""

_EXPERT_B_SKILL = """---
name: expert-b
description: 专家B
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 专家B EXPERT_B_MARK
你会先用 request_user_input 询问，再据答复给结论。
"""

_ORCH_SKILL = """---
name: orchestrator
description: 编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [expert-a, expert-b]
tool_names: []
max_call_depth: 3
---
# 编排器 ORCH_MARK
并发派发专家，错峰收口。
"""


@pytest.fixture
def expert_skills(tmp_path):
    """两个 HITL 专家（expert-a / expert-b，非 entry）+ orchestrator（entry）skills 目录。"""
    skills = tmp_path / "expert_skills"
    for sub, body in (
        ("expert-a", _EXPERT_A_SKILL),
        ("expert-b", _EXPERT_B_SKILL),
        ("orchestrator", _ORCH_SKILL),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


@pytest.mark.asyncio
async def test_spawn_staggered_hitl(expert_skills, threads_dir):
    """A: spawn→HITL挂起→Resume(A)→完成; 之后 B: spawn→HITL→Resume(B)→完成. 错峰、互不耦合。"""
    import taifeng
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient
    from taifeng.loop.submission import Resume
    from taifeng.suspend.record import SuspensionRecord
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    # 每个专家：turn1 调 request_user_input（挂起）；Resume 后 turn2 出最终文本。
    client = RoutingMockClient(routes={
        "EXPERT_A_MARK": [
            MockTurn(text="A 向用户提问", tool_calls=[
                {"id": "call_a", "name": "request_user_input",
                 "arguments": '{"prompt": "A 需要补充信息"}'},
            ]),
            MockTurn(text="A 最终结论 A_DONE"),
        ],
        "EXPERT_B_MARK": [
            MockTurn(text="B 向用户提问", tool_calls=[
                {"id": "call_b", "name": "request_user_input",
                 "arguments": '{"prompt": "B 需要补充信息"}'},
            ]),
            MockTurn(text="B 最终结论 B_DONE"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=expert_skills,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(
        session_id="staggered-hitl", entry_skill_id="orchestrator")

    # subscribe_all：捕获 spawn_suspended / spawn_completed（含 handle_id / thread_id / pending）
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    def _find(kind: str, handle_id: str):
        for ev in events:
            if ev.msg.kind == kind and ev.msg.data.get("handle_id") == handle_id:
                return ev
        return None

    async def _drive_to_done(handle_id: str, child_tid: str) -> None:
        """等待该 handle 挂起 → 取 request_id → Resume(子 thread) → 等其 spawn_completed。"""
        # 1. 等挂起事件
        assert await _wait(lambda: _find("spawn_suspended", handle_id) is not None), \
            f"{handle_id} 未挂起"
        susp = _find("spawn_suspended", handle_id)
        assert susp.msg.data["thread_id"] == child_tid
        # 2. 从子 thread 的 suspension record 取 request_id（DATA：request_id == call_id）
        items = [it async for it in await pool.store.load_thread(child_tid)]
        rec_items = [it for it in items if it.kind == "suspension"]
        assert len(rec_items) == 1
        rec = SuspensionRecord.from_item(rec_items[0])
        req_id = rec.pending[0].request_id
        # 3. Resume(子 thread) 回填表单答案
        await engine.submit(Resume(
            thread_id=child_tid, resolutions={req_id: {"answer": "ok"}}))
        # 4. 等该 handle 的 spawn_completed
        assert await _wait(
            lambda: engine.spawn_status([handle_id])[handle_id]["status"] == "done"), \
            f"{handle_id} 未完成"

    # === A：spawn → 挂起 → Resume → 完成 ===
    a = await engine.spawn_skill(skill_id="expert-a", args={}, reason="A")
    a_hid, a_tid = a["handle_id"], a["child_thread_id"]
    await _drive_to_done(a_hid, a_tid)
    assert _find("spawn_completed", a_hid) is not None, "A 应有 spawn_completed"
    assert engine.spawn_status([a_hid])[a_hid]["result"] == "A 最终结论 A_DONE"

    # === 断言错峰：A 完成时 B 尚未发起（B 的句柄此刻不存在） ===
    assert _find("spawn_started", "ignored") is None  # 占位无意义，仅强调下面才发 B

    # === B：A 完成之后才发起 → 挂起 → Resume → 完成 ===
    b = await engine.spawn_skill(skill_id="expert-b", args={}, reason="B")
    b_hid, b_tid = b["handle_id"], b["child_thread_id"]
    await _drive_to_done(b_hid, b_tid)
    assert _find("spawn_completed", b_hid) is not None, "B 应有 spawn_completed"
    assert engine.spawn_status([b_hid])[b_hid]["result"] == "B 最终结论 B_DONE"

    # === 互不耦合：resume B 不影响 A 的终态，反之亦然 ===
    assert engine.spawn_status([a_hid])[a_hid]["status"] == "done"
    assert engine.spawn_status([b_hid])[b_hid]["status"] == "done"

    watch_task.cancel()
    await pool.close()


# ---------------------------------------------------------------------------
# Task 7: spawn_status(非阻塞) / kill_spawn(单 spawn 隔离取消) /
#         has_live_spawns(引用计数保活：有未终结 spawn 不释放 engine)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_skill_nonblocking_and_kill_isolates(skills_dir, threads_dir):
    """spawn_status 非阻塞读;kill_spawn 未知句柄显式 KeyError(不静默)。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A done"), MockTurn(text="B done")],
        "code-reviewer": [MockTurn(text="主")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s3", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    st = engine.spawn_status([a])  # 非阻塞:running 或 done
    assert a in st and st[a]["status"] in ("running", "done", "suspended")
    with pytest.raises(KeyError):
        await engine.kill_spawn("nope")  # 未知 handle 显式报错
    await pool.close()


@pytest.mark.asyncio
async def test_has_live_spawns_keepalive(expert_skills, threads_dir):
    """有未终结(suspended)spawn 时 engine.has_live_spawns()==True 且 engine 不被释放;
    kill 后全终结 → False。同 session get_or_create 始终返回同一实例(未被 evict)。"""
    import taifeng
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    # 专家 turn1 调 request_user_input → 挂起(非终态);永不 resume → 保持 live。
    client = RoutingMockClient(routes={
        "EXPERT_A_MARK": [
            MockTurn(text="A 提问", tool_calls=[
                {"id": "call_a", "name": "request_user_input",
                 "arguments": '{"prompt": "需要补充"}'},
            ]),
            MockTurn(text="A 最终"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=expert_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(
        session_id="keepalive", entry_skill_id="orchestrator")

    out = await engine.spawn_skill(skill_id="expert-a", args={}, reason="A")
    hid = out["handle_id"]
    # 等子 turn 跑到挂起(suspended,非终态)
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), \
        "spawn 未挂起"
    # 有未终结 spawn → 保活
    assert engine.has_live_spawns() is True
    # 同 session 复用:仍返回同一 engine(未被 evict)
    same = await pool.get_or_create(
        session_id="keepalive", entry_skill_id="orchestrator")
    assert same is engine

    # kill 该 spawn → 转终态 cancelled
    await engine.kill_spawn(hid)
    assert engine.spawn_status([hid])[hid]["status"] == "cancelled"
    # 全终结 → 不再保活
    assert engine.has_live_spawns() is False

    # kill 已终结句柄是良性 no-op(不报错)
    await engine.kill_spawn(hid)
    await pool.close()


# ---------------------------------------------------------------------------
# Task 8: join-barrier —— {句柄集}全终态 → 自动起聚合(联合会诊)turn。
#         失败/取消的专家不被静默丢弃,聚合输入含其终态。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_barrier_fires_when_all_done(skills_dir, threads_dir):
    """两个 spawn 全 done → barrier 自动起聚合 turn,emit join_barrier_fired。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="结论A"), MockTurn(text="结论B")],
        "code-reviewer": [MockTurn(text="会诊:综合A+B")],  # 聚合 skill 用 code-reviewer 占位
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="s4", entry_skill_id="code-reviewer")
    fired = {"v": None}

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired["v"] = dict(ev.msg.data)
                return

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="x"))["handle_id"]
    await engine.set_join_barrier([a, b], then_skill_id="code-reviewer")
    assert await _wait(lambda: fired["v"] is not None)
    assert "then_thread_id" in fired["v"]
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_join_barrier_with_failed_expert(expert_skills, threads_dir):
    """一个专家挂起后被 kill(cancelled 终态)→ barrier 仍触发,聚合输入含取消终态。

    确定性制造非 done 终态:expert-a 首 turn 调 request_user_input → 挂起(永不
    resume,故不会 done),再 kill 它 → cancelled(终态);expert-b 直接跑完 → done。
    两 handle 全终态 → barrier 触发。断言默认聚合参数里**含被取消 handle 的
    cancelled 状态**(失败/取消专家不被静默丢弃,会诊需看到全部专家结局)。
    """
    import json

    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    client = RoutingMockClient(routes={
        # expert-a:首 turn 挂起(request_user_input),永不 resume → 等被 kill
        "EXPERT_A_MARK": [
            MockTurn(text="A 提问", tool_calls=[
                {"id": "call_a", "name": "request_user_input",
                 "arguments": '{"prompt": "需要补充"}'},
            ]),
        ],
        # expert-b:直接给结论 → done
        "EXPERT_B_MARK": [MockTurn(text="B 结论 B_DONE")],
        # orchestrator 作聚合 skill 占位(独立根 turn,无 entry 门控也允许 entry)
        "ORCH_MARK": [MockTurn(text="会诊综合")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=expert_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(
        session_id="jb-fail", entry_skill_id="orchestrator")
    fired = {"v": None}

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired["v"] = dict(ev.msg.data)
                return

    task = asyncio.create_task(watch())
    a = (await engine.spawn_skill(skill_id="expert-a", args={}, reason="A"))["handle_id"]
    b = (await engine.spawn_skill(skill_id="expert-b", args={}, reason="B"))["handle_id"]
    # 等 a 挂起(非终态)、b 跑完(done)
    assert await _wait(lambda: engine.spawn_status([a])[a]["status"] == "suspended")
    assert await _wait(lambda: engine.spawn_status([b])[b]["status"] == "done")
    # kill a → cancelled(终态)。此时两 handle 全终态。
    await engine.kill_spawn(a)
    assert engine.spawn_status([a])[a]["status"] == "cancelled"
    await engine.set_join_barrier([a, b], then_skill_id="orchestrator")
    assert await _wait(lambda: fired["v"] is not None)
    then_tid = fired["v"]["then_thread_id"]

    # 聚合 turn 的种子 user_message 应含【两个 handle】的终态(取消的 a 不丢)
    items = [it async for it in await pool.store.load_thread(then_tid)]
    seed = items[0]
    assert seed.kind == "user_message"
    payload = json.loads(seed.payload["text"])
    assert a in payload and b in payload
    assert payload[a]["status"] == "cancelled"  # 失败/取消专家终态被带入,非静默丢
    assert payload[b]["status"] == "done"
    task.cancel()
    await pool.close()


# ---------------------------------------------------------------------------
# spawn-terminal-single-convergence: _settle_failed 失败终态唯一收敛点。
#   终态幂等(已终态句柄 no-op、终态事件恰好一次)+ barrier 故障隔离
#   (suppress 兜底场景仅记日志;正常控制流向上抛,禁 silent fallback)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_failed_idempotent_on_terminal(expert_skills, threads_dir):
    """_settle_failed 终态幂等:已 cancelled 句柄二次失败收敛 no-op——状态不被
    覆盖为 error、不再 emit 第二条终态事件(spec: 失败收敛终态幂等)。"""
    from taifeng.tool.builtins.request_user_input import (
        make_request_user_input_tool,
    )

    client = RoutingMockClient(routes={
        "EXPERT_A_MARK": [
            MockTurn(text="A 提问", tool_calls=[
                {"id": "call_a", "name": "request_user_input",
                 "arguments": '{"prompt": "需要补充"}'}]),
        ],
        "ORCH_MARK": [MockTurn(text="orch idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=expert_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(
        session_id="settle-idem", entry_skill_id="orchestrator")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    a = (await engine.spawn_skill(
        skill_id="expert-a", args={}, reason="A"))["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([a])[a]["status"] == "suspended")
    await engine.kill_spawn(a)  # suspended-kill → cancelled 终态
    assert engine.spawn_status([a])[a]["status"] == "cancelled"
    # 已终态句柄二次失败收敛 → no-op(白盒直调唯一收敛点)
    await engine._spawn._settle_failed(a, "boom")  # noqa: SLF001
    assert engine.spawn_status([a])[a]["status"] == "cancelled"
    await asyncio.sleep(0.05)
    assert not any(
        m.kind == "spawn_failed" and m.data.get("handle_id") == a
        for m in events), "已终态句柄不得再 emit spawn_failed"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


@pytest.mark.asyncio
async def test_settle_failed_barrier_error_isolation(expert_skills, threads_dir):
    """_settle_failed 的 barrier 故障隔离:suppress(except 兜底场景)下重查
    抛错仅记日志不外抛——句柄已落 error、spawn_failed 已发;非 suppress
    (正常控制流,如 abort 裁决)下抛错向上传播(禁 silent fallback)。"""
    from taifeng.tool.builtins.request_user_input import (
        make_request_user_input_tool,
    )

    ask = {"id": "call_a", "name": "request_user_input",
           "arguments": '{"prompt": "需要补充"}'}
    client = RoutingMockClient(routes={
        # 两次 spawn 各消费一个挂起 turn
        "EXPERT_A_MARK": [MockTurn(text="问1", tool_calls=[dict(ask)]),
                          MockTurn(text="问2", tool_calls=[dict(ask, id="c2")])],
        "ORCH_MARK": [MockTurn(text="orch idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=expert_skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(
        session_id="settle-iso", entry_skill_id="orchestrator")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    a = (await engine.spawn_skill(
        skill_id="expert-a", args={}, reason="A"))["handle_id"]
    b = (await engine.spawn_skill(
        skill_id="expert-a", args={}, reason="B"))["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([a])[a]["status"] == "suspended")
    assert await _wait(
        lambda: engine.spawn_status([b])[b]["status"] == "suspended")
    driver = engine._spawn  # noqa: SLF001

    # 注入 barrier 重查故障(模拟聚合 skill 随 snapshot 热更消失)
    async def boom(changed_handle_id=None):
        raise RuntimeError("join_barrier_skill_missing: gone")

    driver._check_barriers = boom  # noqa: SLF001
    # suppress(兜底场景):不外抛;句柄已收敛 error、spawn_failed 已发
    await driver._settle_failed(a, "crash", suppress_barrier_errors=True)
    assert engine.spawn_status([a])[a]["status"] == "error"
    await asyncio.sleep(0.05)
    assert any(m.kind == "spawn_failed" and m.data.get("handle_id") == a
               for m in events)
    # 非 suppress(正常控制流):向上抛;句柄状态/事件仍已完成(故障仅在重查)
    with pytest.raises(RuntimeError, match="join_barrier_skill_missing"):
        await driver._settle_failed(b, "crash2")
    assert engine.spawn_status([b])[b]["status"] == "error"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


# ---------------------------------------------------------------------------
# Task 9: LLM 入口 —— spawn_skill / await_skills / join_skill / kill_skill 四工具。
#         LLM 在一个 turn 内调用工具发起 detached spawn，工具转发到 engine spawn API。
# ---------------------------------------------------------------------------

# spawn-orch：编排型 entry skill，child_skills 含 style-checker，
# tool_names 声明 4 个 spawn 工具（业务侧以 extra_tools 注入）。
_SPAWN_ORCH_SKILL = """---
name: spawn-orch
description: 分离式编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: [spawn_skill, await_skills, join_skill, kill_skill]
max_call_depth: 3
---
# 分离式编排器 SPAWN_ORCH_MARK
你是编排器，按需分离发起子 skill。
"""

_SPAWN_ORCH_CHILD = """---
name: style-checker
description: 代码风格审查
version: 1.0.0
type: atomic
---
# 风格审查
按规范审查 diff。
"""


@pytest.fixture
def spawn_orch_skills(tmp_path):
    """spawn-orch（entry）+ style-checker（atomic）双 skill 目录。"""
    skills = tmp_path / "orch_skills"
    (skills / "spawn-orch").mkdir(parents=True)
    (skills / "spawn-orch" / "SKILL.md").write_text(
        _SPAWN_ORCH_SKILL, encoding="utf-8")
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(
        _SPAWN_ORCH_CHILD, encoding="utf-8")
    return skills


@pytest.mark.asyncio
async def test_llm_spawn_via_tools(spawn_orch_skills, threads_dir):
    """LLM 在 turn 内连发两个 spawn_skill tool_call → 两个 detached spawn 起飞并跑完。

    orchestrator 首 turn 吐两个 spawn_skill 调用、次 turn 收尾；工具 handler 经
    ctx.extras['spawn_coordinator'] 转发到 engine.spawn_skill。断言：看到两条
    spawn_started 事件，且两个 spawn 句柄最终都到达 done。
    """
    from taifeng.tool.builtins.spawn_skill import (
        make_await_skills_tool,
        make_join_skill_tool,
        make_kill_skill_tool,
        make_spawn_skill_tool,
    )

    client = RoutingMockClient(routes={
        # orchestrator：首 turn 两个 spawn_skill，次 turn 收尾文本
        "SPAWN_ORCH_MARK": [
            MockTurn(text="发起两个并发分析", tool_calls=[
                {"id": "sp_call_1", "name": "spawn_skill",
                 "arguments":
                    '{"skill_id":"style-checker","reason":"a","args":{}}'},
                {"id": "sp_call_2", "name": "spawn_skill",
                 "arguments":
                    '{"skill_id":"style-checker","reason":"b","args":{}}'},
            ]),
            MockTurn(text="已发起，编排结束"),
        ],
        # 两个子 spawn 各跑出一条结论
        "style-checker": [MockTurn(text="结论A"), MockTurn(text="结论B")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=spawn_orch_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[
            make_spawn_skill_tool(),
            make_await_skills_tool(),
            make_join_skill_tool(),
            make_kill_skill_tool(),
        ])
    engine = await pool.get_or_create(
        session_id="task9", entry_skill_id="spawn-orch")

    spawn_started: list[dict] = []

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_started":
                spawn_started.append(dict(ev.msg.data))

    task = asyncio.create_task(watch())
    sub_id = await engine.submit(taifeng.UserMessage(text="分析这段代码"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break

    # 两条 spawn_started 事件（LLM 连发两个 spawn_skill 工具调用）
    assert await _wait(lambda: len(spawn_started) == 2)
    handle_ids = [d["handle_id"] for d in spawn_started]
    assert len(handle_ids) == 2
    # 两个 spawn 最终都 done
    assert await _wait(
        lambda: all(
            engine.spawn_status(handle_ids)[h]["status"] == "done"
            for h in handle_ids
        )
    )
    results = {
        engine.spawn_status([h])[h]["result"] for h in handle_ids
    }
    assert results == {"结论A", "结论B"}
    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_cold_recovery_rebuilds_handles_and_barrier(skills_dir, threads_dir):
    """冷恢复:engine 释放后同 session 重载,从 parent thread 持久项重建句柄表。

    重载后新 engine 扫描 parent thread 的 spawn 锚 → 重建句柄,再据各子 thread
    的终态(done/suspended/中断)推断状态。两个已 done 的 spawn 重载后仍判 done。
    """
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A"), MockTurn(text="B")],
        "code-reviewer": [MockTurn(text="会诊")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="cold1", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="x"))["handle_id"]
    b = (await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="x"))["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([a, b]).get(a, {}).get("status") == "done"
        and engine.spawn_status([b])[b]["status"] == "done")
    parent_tid = engine.thread_id

    # 释放 engine(模拟冷态)——两 spawn 已 done → has_live_spawns()==False → 正常释放
    await pool.release("cold1")

    # 重新加载同 session → 新 engine 从 store 重建(走 resume_thread_id 物化历史)
    engine2 = await pool.get_or_create(
        session_id="cold1", entry_skill_id="code-reviewer",
        resume_thread_id=parent_tid)
    assert engine2 is not engine  # 确实是新实例,而非 cache 命中
    st = engine2.spawn_status([a, b])
    assert st[a]["status"] == "done" and st[b]["status"] == "done"
    await pool.close()


@pytest.mark.asyncio
async def test_cold_recovery_barrier_idempotent(skills_dir, threads_dir):
    """冷恢复幂等:已触发的 barrier 重载后不二次触发(从 fired 标记重建守卫集)。"""
    client = RoutingMockClient(routes={
        "style-checker": [MockTurn(text="A"), MockTurn(text="B")],
        "code-reviewer": [MockTurn(text="会诊1"), MockTurn(text="会诊2")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="cold2", entry_skill_id="code-reviewer")
    a = (await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="x"))["handle_id"]
    b = (await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="x"))["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([a, b]).get(a, {}).get("status") == "done"
        and engine.spawn_status([b])[b]["status"] == "done")
    res = await engine.set_join_barrier([a, b], then_skill_id="code-reviewer")
    barrier_id = res["barrier_id"]
    # 等 barrier 触发(落 join_barrier_fired 标记)
    assert await _wait(lambda: barrier_id in engine._fired_barriers)  # noqa: SLF001
    parent_tid = engine.thread_id

    await pool.release("cold2")

    # 重载:从 join_barrier_fired 标记重建 _fired_barriers → 不二次触发
    engine2 = await pool.get_or_create(
        session_id="cold2", entry_skill_id="code-reviewer",
        resume_thread_id=parent_tid)
    assert barrier_id in engine2._fired_barriers  # noqa: SLF001
    # 重载后 parent thread 不应出现第二条 join_barrier_fired 标记
    items = [it async for it in await pool.store.load_thread(parent_tid)]
    fired_markers = [
        it for it in items
        if it.kind == "join_barrier_fired"
        and it.payload.get("barrier_id") == barrier_id
    ]
    assert len(fired_markers) == 1
    await pool.close()


# ---------------------------------------------------------------------------
# Task 11: spawn / barrier 拒绝路径 + K1 配额（禁 silent fallback）。
#          每条非法入参都必须显式 raise，绝不静默降级。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_rejections(skills_dir, threads_dir):
    """spawn / barrier / kill 的全部拒绝路径都显式 raise（不静默）。

    覆盖：
      - spawn_skill 未知 skill → ValueError(unknown_skill)
      - set_join_barrier 句柄集含未注册句柄 → ValueError(unknown_spawn_handle)
      - set_join_barrier then_skill_id 不在 snapshot → ValueError(unknown_skill)
      - kill_spawn 未知句柄 → KeyError
    spawn_status 未知句柄是只读、返回 {"status":"unknown"} 不 raise（不在此测）。
    """
    client = RoutingMockClient(routes={
        "code-reviewer": [MockTurn(text="主")],
        "style-checker": [MockTurn(text="x")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="rej", entry_skill_id="code-reviewer")

    # 1. 未知 skill —— spawn 必须显式报错（不静默 no-op）
    with pytest.raises(ValueError, match="unknown_skill"):
        await engine.spawn_skill(skill_id="ghost", args={}, reason="x")

    # 合法 spawn 一个 style-checker（在白名单内）作为后续 barrier 句柄
    a = (await engine.spawn_skill(
        skill_id="style-checker", args={}, reason="x"))["handle_id"]

    # 2. barrier 句柄集含未注册句柄 → 显式报错
    with pytest.raises(ValueError, match="unknown_spawn_handle"):
        await engine.set_join_barrier(
            [a, "unknown-handle"], then_skill_id="code-reviewer")

    # 3. barrier then_skill_id 不在 snapshot → 显式报错
    with pytest.raises(ValueError, match="unknown_skill"):
        await engine.set_join_barrier([a], then_skill_id="does-not-exist")

    # 4. kill 未知句柄 → 显式 KeyError（与 spawn_status 只读未知不同：写操作必报错）
    with pytest.raises(KeyError):
        await engine.kill_spawn("nope")

    await pool.close()


@pytest.mark.asyncio
async def test_spawn_not_in_whitelist_rejected(skills_dir, threads_dir):
    """spawn 一个存在但不在 caller 白名单的 skill → dispatch_rejected(not_in_whitelist)。

    code-reviewer 的 child_skills 仅含 style-checker；尝试 spawn code-reviewer
    本身（存在但不在自身白名单，且为 entry）→ DispatchPolicy.check 拒绝 → 显式报错。
    """
    client = RoutingMockClient(routes={
        "code-reviewer": [MockTurn(text="主")],
        "style-checker": [MockTurn(text="x")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[])
    engine = await pool.get_or_create(
        session_id="rej-wl", entry_skill_id="code-reviewer")

    # code-reviewer 存在但不在自身 child_skills 白名单（且 entry）→ 派发被拒
    with pytest.raises(ValueError, match="dispatch_rejected"):
        await engine.spawn_skill(
            skill_id="code-reviewer", args={}, reason="x")

    await pool.close()


# K1 配额测试专用 skill：tool-only 专家，turn1 调一个会阻塞的工具 hold_slot，
# 子 turn 一直卡在工具内（status=running，K1 slot 由 _drive_spawn 持有），
# 直到测试 set 释放事件。比 suspend 路径更稳：suspend 会让 runner.run() 返回 →
# _drive_spawn finally 释放 slot（slot 不再被占），无法稳定制造并发占用。
_HOLD_EXPERT_A = """---
name: hold-a
description: 占位专家A
version: 1.0.0
type: composite
model: mock-model
tool_names: [hold_slot]
max_call_depth: 2
---
# 占位专家A HOLD_A_MARK
调 hold_slot 长占。
"""

_HOLD_EXPERT_B = """---
name: hold-b
description: 占位专家B
version: 1.0.0
type: composite
model: mock-model
tool_names: [hold_slot]
max_call_depth: 2
---
# 占位专家B HOLD_B_MARK
调 hold_slot 长占。
"""

_HOLD_ORCH = """---
name: hold-orch
description: 占位编排器
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [hold-a, hold-b]
tool_names: []
max_call_depth: 3
---
# 占位编排器 HOLD_ORCH_MARK
并发派发占位专家。
"""


@pytest.fixture
def hold_skills(tmp_path):
    """hold-a / hold-b（tool-only，调阻塞工具长占 slot）+ hold-orch（entry）。"""
    skills = tmp_path / "hold_skills"
    for sub, body in (
        ("hold-a", _HOLD_EXPERT_A),
        ("hold-b", _HOLD_EXPERT_B),
        ("hold-orch", _HOLD_ORCH),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


@pytest.mark.asyncio
async def test_spawn_quota_rejected(hold_skills, threads_dir):
    """K1 配额：max_concurrent_spawns=1，首个 spawn 卡在阻塞工具占住唯一 slot，
    第二个超限被显式拒（SpawnLimitError，不静默丢弃）。

    确定性关键：mock spawn 跑得太快会瞬间完成、释放 slot；suspend 路径同样会让
    runner.run() 返回从而释放 slot。故注入一个 hold_slot 工具——它在被取消前一直
    await，使首个 spawn 的子 turn 停在 running（slot 由 _drive_spawn 持续持有）。
    此时第二个 spawn 的 reserve_manual 必触发 SpawnLimitError(concurrent>=1)。
    """
    from taifeng.loop.spawn import SpawnLimitError
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    # 阻塞工具：被调后挂在 _release 事件上，直到测试显式释放（或被取消）。
    # 借此把首个 spawn 的子 turn 钉在 running，稳定占住 K1 slot。
    release = asyncio.Event()
    entered = asyncio.Event()

    async def _hold_slot(args, ctx: ToolContext) -> ToolResult:
        """长占工具：标记已进入 → 等待释放事件（可被 cancel）。"""
        entered.set()
        await release.wait()
        return ToolResult.ok("released")

    hold_tool = ToolSpec(
        name="hold_slot",
        description="测试用：长占一个 spawn slot 直到被释放",
        input_schema={"type": "object", "properties": {}},
        handler=_hold_slot,
        parallel_safe=False,
        timeout_seconds=30.0,
    )

    client = RoutingMockClient(routes={
        "HOLD_A_MARK": [
            MockTurn(text="A 长占", tool_calls=[
                {"id": "call_a", "name": "hold_slot", "arguments": "{}"},
            ]),
            MockTurn(text="A 完成"),
        ],
        "HOLD_B_MARK": [
            MockTurn(text="B 长占", tool_calls=[
                {"id": "call_b", "name": "hold_slot", "arguments": "{}"},
            ]),
            MockTurn(text="B 完成"),
        ],
    })
    # K1：并发上限设为 1（knob 真名为 max_concurrent_spawns，透传到 SpawnSlotRegistry）
    pool = await taifeng.EnginePool.create(
        skills_dir=hold_skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[hold_tool],
        max_concurrent_spawns=1)
    engine = await pool.get_or_create(
        session_id="quota", entry_skill_id="hold-orch")

    # 首个 spawn：子 turn 进入 hold_slot 并阻塞 → 一直 running，占住唯一 slot。
    out = await engine.spawn_skill(skill_id="hold-a", args={}, reason="A")
    hid = out["handle_id"]
    assert await _wait(entered.is_set), "首个 spawn 未进入阻塞工具，无法占住 K1 slot"
    assert engine.spawn_status([hid])[hid]["status"] == "running"

    # 第二个 spawn：并发已达上限 1 → reserve_manual 抛 SpawnLimitError(concurrent)
    with pytest.raises(SpawnLimitError) as exc:
        await engine.spawn_skill(skill_id="hold-b", args={}, reason="B")
    assert exc.value.kind == "concurrent" and exc.value.limit == 1

    # 释放阻塞工具 → 首个 spawn 跑完转 done（slot 归还，不泄漏）。
    release.set()
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done")
    assert engine._spawn_registry.snapshot()["active"] == 0  # noqa: SLF001

    await pool.close()


@pytest.mark.asyncio
async def test_kill_running_spawn_emits_exactly_one_cancelled(
    hold_skills, threads_dir
):
    """kill 一个 RUNNING spawn → 恰好一条 spawn_cancelled 且终态 cancelled。

    回归点（终态幂等单点收敛）：running 句柄被 kill 时，kill_spawn 仅取消 token、
    不内联 emit；被取消的 live runner 退栈后 _drive_spawn 调 _finalize_spawn 唯一
    一次 emit SpawnCancelled。若 kill_spawn 也内联 emit，则同一句柄会收到两条
    spawn_cancelled（消费方重复计数）。

    用 hold_slot 阻塞工具把 spawn 钉在 running（与 test_spawn_quota_rejected 同法），
    确保 kill 时句柄确为 running（有 live 子树），从而走 _finalize_spawn 收敛路径。
    """
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    # 阻塞工具：进入后等待 token 取消（kill 取消子树 token 时被唤醒并抛 CancelledError）。
    # 取消是协作式：工具须主动 race 在 ctx.cancel.wait_cancelled() 上，kill 才能中断它，
    # 使 runner 退栈 → end_reason=cancelled → _drive_spawn 调 _finalize_spawn 收敛。
    entered = asyncio.Event()

    async def _hold_slot(args, ctx: ToolContext) -> ToolResult:
        """长占工具：标记已进入 → 等待本 spawn 子树取消，被取消即抛 CancelledError。"""
        entered.set()
        await ctx.cancel.wait_cancelled()
        ctx.cancel.raise_if_cancelled()  # 被 kill 取消 → 抛 CancelledError，turn 退栈
        return ToolResult.ok("unreachable")

    hold_tool = ToolSpec(
        name="hold_slot",
        description="测试用：长占直到被释放/取消",
        input_schema={"type": "object", "properties": {}},
        handler=_hold_slot,
        parallel_safe=False,
        timeout_seconds=30.0,
    )

    client = RoutingMockClient(routes={
        "HOLD_A_MARK": [
            MockTurn(text="A 长占", tool_calls=[
                {"id": "call_a", "name": "hold_slot", "arguments": "{}"},
            ]),
            MockTurn(text="A 完成"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=hold_skills, threads_dir=threads_dir,
        model_client=client, compressors=[], extra_tools=[hold_tool])
    engine = await pool.get_or_create(
        session_id="kill-running", entry_skill_id="hold-orch")

    # 订阅全部事件，统计该句柄的 spawn_cancelled 条数。
    cancelled_events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_cancelled":
                cancelled_events.append(dict(ev.msg.data))
            if ev.msg.kind == "shutdown":
                break

    watch_task = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 subscribe_all 注册队列

    out = await engine.spawn_skill(skill_id="hold-a", args={}, reason="A")
    hid = out["handle_id"]
    # 等子 turn 进入阻塞工具 → 句柄确为 running（有 live 子树）。
    assert await _wait(entered.is_set), "spawn 未进入阻塞工具，无法钉在 running"
    assert engine.spawn_status([hid])[hid]["status"] == "running"

    # kill running spawn：token 取消 → runner 退栈 → _finalize_spawn 唯一一次 emit。
    await engine.kill_spawn(hid)
    # 终态收敛到 cancelled。
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "cancelled"), \
        "running spawn kill 后未收敛到 cancelled"

    # 关键断言：恰好一条 spawn_cancelled（无双发）。给后台 finalize 充分时间后再核。
    assert await _wait(
        lambda: len([d for d in cancelled_events if d["handle_id"] == hid]) >= 1)
    # 再等一拍，确认不会出现第二条。
    for _ in range(20):
        await asyncio.sleep(0.01)
    mine = [d for d in cancelled_events if d["handle_id"] == hid]
    assert len(mine) == 1, f"应恰好一条 spawn_cancelled，实际 {len(mine)} 条"

    watch_task.cancel()
    await pool.close()
