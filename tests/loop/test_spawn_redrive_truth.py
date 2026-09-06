"""Wave 2b 复现:detached spawn 二次驱动 / 冷推断 / 续跑链取消的热冷真相。

对应 openspec change ``wave2b-spawn-resume-truth``。每条用例先于修复写出,
改动前必须 FAIL(失败原因见 change tasks 1.1):
  a) rewind 后 peer 唤醒:二次驱动读 raw → prompt 混入被截掉的旧派发与 [rewind] marker
  b) 冷恢复:挂起态 spawn 子 thread 的过期 TTL 从不武装(重武装早于句柄重建)
  c) resume 窗口内 kill:set_result(running) 覆盖 cancelled 终态 → 双终态事件 + runner 跑在已 kill 句柄上
  d) max_concurrent_spawns=1 下 resume 不占 K1 slot 直接起跑
  e) suspended-kill 只改内存 → 冷恢复复活为 suspended / 被 TTL 裁决
  f) error 终态无持久锚 → 冷恢复凭 assistant 文本误判 done
  g) 续跑链 leaf 被 CancelTurn 后父 / 根继续采样(链不停)
"""
from __future__ import annotations

import asyncio
import time

import pytest

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import CancelTurn, Resume, Rewind
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

_HOST = """---
name: host
description: 宿主入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker, expert]
max_call_depth: 3
---
# 宿主 HOST_MARK
派发专家。
"""

_WORKER = """---
name: worker
description: 可派发 echo / 可被门控钉住的工作者
version: 1.0.0
type: composite
model: mock-model
tool_names: [echo, gate_wait]
max_call_depth: 2
---
# 工作者 WORKER_MARK
按需调用工具。
"""

_EXPERT = """---
name: expert
description: 问询专家
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 专家 EXPERT_MARK
先问人再下结论。
"""


@pytest.fixture
def redrive_skills(tmp_path):
    """host(entry) + worker(echo / gate_wait) + expert(request_user_input)。"""
    skills = tmp_path / "redrive_skills"
    for sub, body in (("host", _HOST), ("worker", _WORKER), ("expert", _EXPERT)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _echo_tool() -> ToolSpec:
    """极简 echo 工具(产生 dispatch 节点用)。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True,
    )


def _gate_tool(gate: asyncio.Event) -> ToolSpec:
    """门控工具:handler 阻塞到 gate set —— 把子 turn 钉在「运行中」占住 K1 slot。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        await gate.wait()
        return ToolResult.ok("opened")

    return ToolSpec(
        name="gate_wait", description="等门",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True,
    )


def _future_now() -> int:
    """注入的壁钟:真实时间 + 1 小时 → 任何 ≤3600s 的 ttl 装载即过期。"""
    return int(time.time()) + 3600


def _ask_turns(final_text: str) -> list[SimTurn]:
    """专家剧本:先 request_user_input 挂起,Resume 后出 final_text。"""
    return [
        SimTurn(text="问", tool_calls=[
            {"id": "q1", "name": "request_user_input",
             "arguments": '{"prompt": "补充?"}'}]),
        SimTurn(text=final_text),
    ]


async def _make_pool(skills, threads_dir, routes, *, gate=None, ttl=None, **kwargs):
    """统一建池:RoutingSimClient + echo / gate_wait / request_user_input 三工具。"""
    client = RoutingSimClient(routes=routes)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[
            _echo_tool(), _gate_tool(gate or asyncio.Event()),
            make_request_user_input_tool(ttl_seconds=ttl),
        ],
        **kwargs,
    )
    return pool, client


def _status(engine, hid: str) -> str:
    return str(engine.spawn_status([hid])[hid]["status"])


def _watch(engine) -> tuple[list, asyncio.Task]:
    """subscribe_all 收集器(submit 前启动,避免丢首批事件)。"""
    events: list = []

    async def _run() -> None:
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    return events, asyncio.create_task(_run())


async def _active_record(pool, tid: str) -> SuspensionRecord:
    """读子 thread 最新一条 suspension item 为 SuspensionRecord。"""
    items = [it async for it in await pool.store.load_thread(tid)]
    recs = [it for it in items if it.kind == "suspension"]
    assert recs, f"thread {tid} 无 suspension 记录"
    return SuspensionRecord.from_item(recs[-1])


async def _thread_blob(pool, tid: str) -> str:
    items = [it async for it in await pool.store.load_thread(tid)]
    return " ".join(str(it.payload) for it in items)


async def _spawn_until(engine, skill_id: str, want: str) -> tuple[str, str]:
    out = await engine.spawn_skill(skill_id=skill_id, args={}, reason="t")
    hid, ctid = out["handle_id"], out["child_thread_id"]
    await wait_for_condition(
        lambda: _status(engine, hid) == want,
        message=f"spawn {skill_id} 未进入 {want}(实为 {_status(engine, hid)})",
    )
    return hid, ctid


# ───────────── a) 二次驱动必须读逻辑 history ─────────────


async def test_redrive_after_rewind_prompt_excludes_truncated_items(
    redrive_skills, threads_dir,
) -> None:
    """rewind 重推落 done 后 peer 唤醒:新 runner 的 prompt 不得含被截断的旧派发
    (function_call c1)与 `[rewind]` marker 文本。"""
    pool, client = await _make_pool(redrive_skills, threads_dir, routes={
        "WORKER_MARK": [
            SimTurn(text="第1轮派发。", tool_calls=[
                {"id": "c1", "name": "echo", "arguments": "{}"}]),
            SimTurn(text="重跑成功"),
            SimTurn(text="被唤醒后的补充"),
        ],
        "HOST_MARK": [SimTurn(text="主")],
    }, max_iterations=1)
    engine = await pool.get_or_create(session_id="rd-a", entry_skill_id="host")
    hid, ctid = await _spawn_until(engine, "worker", "error")

    disp = next(
        c for c in await engine.rewind_nodes_for(ctid) if c.kind == "dispatch")
    await engine.submit(
        Rewind(node_id=disp.node_id, thread_id=ctid, mode="re_reason"))
    await wait_for_condition(
        lambda: engine.spawn_status([hid])[hid]["result"] == "重跑成功",
        message="rewind 重推未落 done")

    out = await engine.deliver_peer_message(
        target=ctid, text="请补充意见", mode="trigger_turn",
        from_thread_id=engine.thread_id)
    assert out["woken"] is True
    await wait_for_condition(
        lambda: engine.spawn_status([hid])[hid]["result"] == "被唤醒后的补充",
        message="唤醒 turn 未落终态")

    last = client.ledger.last_request()
    assert last is not None
    blob = last.blob()
    assert "请补充意见" in blob, "最后一次采样应是唤醒 turn"
    assert "[rewind]" not in blob, "被截断的 rewind marker 不应进入二次驱动的 prompt"
    assert not last.saw_function_call("c1"), "被 rewind 截掉的旧派发不应回流到 prompt"
    await pool.close()


# ───────────── b) 冷重武装必须晚于句柄重建 ─────────────


async def test_cold_resume_rearms_ttl_for_suspended_spawn_child(
    redrive_skills, threads_dir,
) -> None:
    """pool#1 中 spawn 子 thread 带 ttl 挂起后关闭;pool#2 注入未来时钟冷恢复父
    thread → 句柄重建后该 record 立即到期裁决(abort → 句柄 error)。"""
    routes = {"EXPERT_MARK": _ask_turns("不应被采样"), "HOST_MARK": [SimTurn(text="主")]}
    pool1, _ = await _make_pool(redrive_skills, threads_dir, routes, ttl=60)
    engine1 = await pool1.get_or_create(session_id="rd-b1", entry_skill_id="host")
    hid, ctid = await _spawn_until(engine1, "expert", "suspended")
    parent_tid = engine1.thread_id
    await pool1.close()

    pool2, _ = await _make_pool(
        redrive_skills, threads_dir, routes, ttl=60, now_factory=_future_now)
    engine2 = await pool2.get_or_create(
        session_id="rd-b2", entry_skill_id="host", resume_thread_id=parent_tid)
    await wait_for_condition(
        lambda: _status(engine2, hid) == "error",
        message="冷恢复后挂起态 spawn 的过期 TTL 应立即裁决(abort → error)")
    items = [it async for it in await pool2.store.load_thread(ctid)]
    fco = [it for it in items if it.kind == "function_call_output"
           and it.payload.get("call_id") == "q1"]
    assert fco and "suspension_expired" in str(fco[-1].payload.get("output"))
    await pool2.close()


# ───────────── c) resume 窗口内 kill 必须是终局 ─────────────


async def test_kill_during_resume_window_is_final(
    redrive_skills, threads_dir,
) -> None:
    """Resume 已开始核销、尚未起 runner 时 kill:句柄终局 cancelled,
    spawn_cancelled 恰好一次,不再有 spawn_completed,子脚本第二轮未被消费。"""
    routes = {"EXPERT_MARK": _ask_turns("EXPERT_RESUMED_TEXT"),
              "HOST_MARK": [SimTurn(text="主")]}
    pool, _ = await _make_pool(redrive_skills, threads_dir, routes)
    engine = await pool.get_or_create(session_id="rd-c", entry_skill_id="host")
    events, watch = _watch(engine)
    await asyncio.sleep(0)
    hid, ctid = await _spawn_until(engine, "expert", "suspended")
    rec = await _active_record(pool, ctid)
    req_id = rec.pending[0].request_id

    # 把 resume 钉在「marker 未落、runner 未起」的窗口(实例属性遮蔽注入点)
    hold, entered = asyncio.Event(), asyncio.Event()
    orig = engine._append_resolved_marker  # noqa: SLF001

    async def gated(thread_id: str, record_id: str) -> None:
        entered.set()
        await hold.wait()
        await orig(thread_id, record_id)

    engine._append_resolved_marker = gated  # noqa: SLF001
    resume_sub = await engine.submit(
        Resume(thread_id=ctid, resolutions={req_id: {"answer": "ok"}}))
    await asyncio.wait_for(entered.wait(), timeout=GUARD_TIMEOUT_SECONDS)

    await engine.kill_spawn(hid)
    assert _status(engine, hid) == "cancelled"
    hold.set()
    # 等 resume operation 收尾(无论它走哪条分支)
    await wait_for_condition(
        lambda: not any(
            t.get_name().endswith(f"resume-spawn:{resume_sub}") and not t.done()
            for t in engine._operation_tasks),  # noqa: SLF001
        message="resume operation 未收尾")

    assert _status(engine, hid) == "cancelled", "kill 后的 resume 不得把句柄改回 running / done"
    cancelled = [e for e in events if e.msg.kind == "spawn_cancelled"
                 and e.msg.data.get("handle_id") == hid]
    completed = [e for e in events if e.msg.kind == "spawn_completed"
                 and e.msg.data.get("handle_id") == hid]
    assert len(cancelled) == 1, f"spawn_cancelled 应恰好一次,实得 {len(cancelled)}"
    assert not completed, "已 kill 的句柄不得再 emit spawn_completed"
    assert "EXPERT_RESUMED_TEXT" not in await _thread_blob(pool, ctid), \
        "已 kill 的句柄不得再起 runner 消费子脚本"
    watch.cancel()
    await pool.close()


# ───────────── d) resume 重推必须占 K1 slot ─────────────


async def test_resume_waits_for_k1_slot(redrive_skills, threads_dir) -> None:
    """max_concurrent_spawns=1:A 挂起(slot 已释放)、B 运行中占满 slot;
    Resume(A) 核销后不得起跑,B 终态后 A 才起跑并落 done。"""
    gate = asyncio.Event()
    routes = {
        "EXPERT_MARK": _ask_turns("A_RESUMED"),
        "WORKER_MARK": [
            SimTurn(text="等门", tool_calls=[
                {"id": "g1", "name": "gate_wait", "arguments": "{}"}]),
            SimTurn(text="门开后"),
        ],
        "HOST_MARK": [SimTurn(text="主")],
    }
    pool, client = await _make_pool(
        redrive_skills, threads_dir, routes, gate=gate, max_concurrent_spawns=1)
    engine = await pool.get_or_create(session_id="rd-d", entry_skill_id="host")
    events, watch = _watch(engine)
    await asyncio.sleep(0)
    a_hid, a_tid = await _spawn_until(engine, "expert", "suspended")
    b_hid, _ = await _spawn_until(engine, "worker", "running")
    assert engine._spawn_registry.snapshot()["active"] == 1  # noqa: SLF001
    rec = await _active_record(pool, a_tid)
    req_id = rec.pending[0].request_id

    await engine.submit(
        Resume(thread_id=a_tid, resolutions={req_id: {"answer": "ok"}}))
    await wait_for_condition(
        lambda: any(e.msg.kind == "suspension_resolved"
                    and e.msg.data.get("record_id") == rec.record_id
                    for e in events),
        message="A 未核销")
    # 缺席断言:满额时 A 不得起跑。窗口只需覆盖「若绕过 K1 会立刻起跑」的调度;
    # 修复后 A 在 B 释放 slot 前结构上不可能起跑,窗口长短不影响判定。
    await asyncio.sleep(0.1)
    assert _status(engine, a_hid) == "suspended", "满额时 resume 不得绕过 K1 起跑"
    assert engine._spawn_registry.snapshot()["active"] == 1  # noqa: SLF001
    expert_samples = sum(
        1 for r in client.ledger.requests() if "EXPERT_MARK" in r.blob())
    assert expert_samples == 1, "A 的第二轮采样不应在 slot 释放前发生"

    gate.set()
    await wait_for_condition(lambda: _status(engine, b_hid) == "done", message="B 未完成")
    await wait_for_condition(
        lambda: engine.spawn_status([a_hid])[a_hid]["result"] == "A_RESUMED",
        message="B 释放 slot 后 A 应自动起跑并落 done")
    watch.cancel()
    await pool.close()


# ───────────── e) suspended-kill 必须落盘 ─────────────


async def test_killed_suspended_spawn_stays_cancelled_after_cold_resume(
    redrive_skills, threads_dir,
) -> None:
    """kill 挂起句柄后冷恢复:推断 cancelled(不复活为 suspended)、不武装 TTL
    (未来时钟下若武装会立即裁决成 error)、Resume 被拒。"""
    routes = {"EXPERT_MARK": _ask_turns("不应被采样"), "HOST_MARK": [SimTurn(text="主")]}
    pool1, _ = await _make_pool(redrive_skills, threads_dir, routes, ttl=60)
    engine1 = await pool1.get_or_create(session_id="rd-e1", entry_skill_id="host")
    hid, ctid = await _spawn_until(engine1, "expert", "suspended")
    rec = await _active_record(pool1, ctid)
    await engine1.kill_spawn(hid)
    assert _status(engine1, hid) == "cancelled"
    parent_tid = engine1.thread_id
    await pool1.close()

    pool2, _ = await _make_pool(
        redrive_skills, threads_dir, routes, ttl=60, now_factory=_future_now)
    engine2 = await pool2.get_or_create(
        session_id="rd-e2", entry_skill_id="host", resume_thread_id=parent_tid)
    assert _status(engine2, hid) == "cancelled", "被 kill 的挂起句柄冷恢复后不得复活"
    assert not engine2._ttl_timers, "被 kill 的句柄不得武装 TTL"  # noqa: SLF001

    events, watch = _watch(engine2)
    await asyncio.sleep(0)
    sub = await engine2.submit(Resume(
        thread_id=ctid,
        resolutions={rec.pending[0].request_id: {"answer": "ok"}}))
    await wait_for_condition(
        lambda: any(e.submission_id == sub
                    and e.msg.kind == "suspension_resolve_rejected"
                    for e in events),
        message="对已 kill 句柄的 Resume 应被显式拒绝")
    assert _status(engine2, hid) == "cancelled"
    watch.cancel()
    await pool2.close()


# ───────────── f) error 终态冷推断 ─────────────


async def test_cold_resume_infers_error_not_done(redrive_skills, threads_dir) -> None:
    """子 thread 有 assistant 文本但以 error 终态结束:冷恢复推断 error,不因
    存在 assistant 文本误判 done。"""
    routes = {
        "WORKER_MARK": [SimTurn(text="第1轮派发。", tool_calls=[
            {"id": "c1", "name": "echo", "arguments": "{}"}])],
        "HOST_MARK": [SimTurn(text="主")],
    }
    pool1, _ = await _make_pool(redrive_skills, threads_dir, routes, max_iterations=1)
    engine1 = await pool1.get_or_create(session_id="rd-f1", entry_skill_id="host")
    hid, _ = await _spawn_until(engine1, "worker", "error")
    parent_tid = engine1.thread_id
    await pool1.close()

    pool2, _ = await _make_pool(redrive_skills, threads_dir, routes, max_iterations=1)
    engine2 = await pool2.get_or_create(
        session_id="rd-f2", entry_skill_id="host", resume_thread_id=parent_tid)
    st = engine2.spawn_status([hid])[hid]
    assert st["status"] == "error", f"error 终态冷恢复应推断 error,实为 {st}"
    await pool2.close()


# ───────────── g) 续跑链取消即停并解链 ─────────────


async def test_cancel_in_resume_chain_stops_parents_and_unblocks_root(
    tmp_path, threads_dir,
) -> None:
    """根 → 子链续跑中 CancelTurn(resume_sub):leaf cancelled 后父 / 根不得再采样;
    Resume submission 以 turn_failed{kind=cancelled} 终结;根解除挂起可接新消息。"""
    from taifeng.permission.types import (
        PermissionPolicy,
        PermissionRule,
        SuspendingPrompter,
    )
    from tests.test_child_suspend_resume import (
        _AllEventsRecorder,
        _build_skills,
        _gated_danger_tool,
    )

    client = RoutingSimClient(routes={
        "ENTRY_MARK": [
            SimTurn(text="派发子 skill", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id": "child-worker", "reason": "do work"}'},
            ]),
            SimTurn(text="父层重新采样 PARENT_RESAMPLED"),
        ],
        "CHILD_MARK": [
            SimTurn(text="子调用 danger", tool_calls=[
                {"id": "call_d1", "name": "danger", "arguments": "{}"},
            ]),
            SimTurn(text="子工作完成 CHILD_DONE_MARK", delay_seconds=1.0),
        ],
    })
    policy = PermissionPolicy(
        default_mode="ask",
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        prompter=SuspendingPrompter(),
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=_build_skills(tmp_path), threads_dir=threads_dir,
        model_client=client, compressors=[],
        extra_tools=[await _gated_danger_tool()], permission_policy=policy,
    )
    try:
        engine = await pool.get_or_create(session_id="rd-g", entry_skill_id="parent-orch")
        recorder = _AllEventsRecorder(engine)
        await wait_for_condition(
            lambda: recorder.registered(engine), message="firehose 订阅未能登记")

        sub_id = await engine.submit(taifeng.UserMessage(text="go"))
        events1 = await recorder.wait_terminal(sub_id)
        suspend_ev = next(ev for ev in events1 if ev.msg.kind == "turn_suspended")
        child_tid = suspend_ev.msg.data["thread_id"]
        rec = await _active_record(pool, child_tid)
        req_id = rec.pending[0].request_id

        resume_sub = await engine.submit(Resume(
            thread_id=child_tid, resolutions={req_id: {"granted": True}}))
        await wait_for_condition(
            lambda: recorder.seen(
                lambda e: e.submission_id == resume_sub
                and e.msg.kind == "turn_started"
                and not e.msg.data.get("is_root")),
            message="子续跑 turn 未起飞")
        await engine.submit(CancelTurn(submission_id=resume_sub))

        events2 = await recorder.wait_terminal(resume_sub)
        child_done = next(
            ev for ev in events2
            if ev.msg.kind == "turn_completed" and not ev.msg.data.get("is_root"))
        assert child_done.msg.data["end_reason"] == "cancelled"
        root_term = next(
            ev for ev in events2
            if ev.msg.kind in ("turn_completed", "turn_failed")
            and ev.msg.data.get("is_root"))
        assert root_term.msg.kind == "turn_failed" and \
            root_term.msg.data.get("kind") == "cancelled", (
            f"链取消后根应以 turn_failed{{cancelled}} 终结,实得 "
            f"{root_term.msg.kind}:{root_term.msg.data}")

        root_items = [it async for it in await pool.store.load_thread(engine.thread_id)]
        root_blob = " ".join(str(it.payload) for it in root_items)
        assert "PARENT_RESAMPLED" not in root_blob, "链取消后父 / 根不得再采样"
        fco = [it for it in root_items if it.kind == "function_call_output"
               and it.payload.get("call_id") == "c_call"]
        assert fco and "cancelled" in str(fco[-1].payload.get("output")), \
            "根的 call_skill gap 应回填 cancelled 错误输出(解链)"
        assert engine._find_active_suspension() is None, "解链后根不得仍挂在 CHILD_SKILL 上"  # noqa: SLF001

        # 根可接新消息(挂起态会拒收 UserMessage)
        sub3 = await engine.submit(taifeng.UserMessage(text="继续"))
        events3 = await recorder.wait_terminal(sub3)
        term3 = next(
            ev for ev in events3
            if ev.msg.kind in ("turn_completed", "turn_failed")
            and ev.msg.data.get("is_root"))
        assert term3.msg.kind == "turn_completed", f"解链后新消息应正常开 turn,实得 {term3.msg.data}"
    finally:
        await pool.close()
