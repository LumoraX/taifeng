"""detached spawn【直接挂起】续跑 —— 边界对抗验证（补 resume_spawn 直接路径盲区）。

resume_spawn / _resume_spawn_settled 处理的是「spawn 子 thread 自身直接挂起」
（DATA / PERMISSION / 失败型落在子 thread 本层，_next_child_link 为 None → 不走嵌套链）。

现有覆盖（不在此重复）：
  - test_failure_policy_chains.py 已覆盖直接路径的**全量核销 retry→done** 与
    **abort→_settle_failed（error + SpawnFailed）**（RESOURCE_LIMIT 失败型挂起）。
  - test_spawn_nested_resume_boundaries.py / test_detached_spawn_nested_hitl.py 覆盖
    **嵌套**链（深度3 / entry-target / join-barrier / 多轮嵌套 HITL）。

本文件补直接路径仍缺的四个高风险盲区：
  1. **部分核销**：子 thread 单 turn 多 pending（danger×2 并发挂起）→ 只 resolve 子集
     → SuspensionPartiallyResolved + 句柄保持 suspended（不续跑）；补齐 → done。
  2. **在飞守卫**：同一 record 已在结算中（_resolving_records 命中）→ 再来一次
     resume_spawn 应 emit SuspensionResolveRejected(resolve_in_flight)、不重复结算。
  3. **无活跃挂起**：句柄虽标 suspended 但子 thread 已无活跃挂起（竞态/已被并发核销）
     → emit SuspensionResolveRejected(no_active_suspension)、不崩溃。
  4. **多轮错峰 DATA HITL**：子 thread 自身连续两轮 request_user_input 挂起 →
     逐轮 Resume → _finalize_spawn 每轮重标 suspended（可再 resume）→ 末轮 done。
"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import Resume, Submission
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待条件成立（每次 10ms；spawn 子 task 调度无确定时序）。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _skill(name: str, *, entry: bool = False, children: list[str] | None = None,
           tools: list[str] | None = None) -> str:
    """生成一个 composite SKILL.md（body 含唯一 MARK = 大写下划线 name + _MARK）。"""
    mark = name.upper().replace("-", "_") + "_MARK"
    lines = ["---", f"name: {name}", f"description: {name}", "version: 1.0.0",
             "type: composite", "model: mock-model"]
    if entry:
        lines.append("entry: true")
    if children:
        lines.append(f"child_skills: [{', '.join(children)}]")
    if tools:
        lines.append(f"tool_names: [{', '.join(tools)}]")
    lines += ["max_call_depth: 3", "---", f"# {name} {mark}", ""]
    return "\n".join(lines)


def _write_skills(root, spec: dict[str, str]):
    """把 {子目录名: SKILL.md 文本} 写进 skills 目录。"""
    for sub, body in spec.items():
        d = root / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _collect_events(engine) -> tuple[list, asyncio.Task]:
    """submit 前启动 subscribe_all 收集器（避免异步 resume 抢跑丢首批事件）。"""
    events: list = []

    async def _run():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    return events, asyncio.create_task(_run())


def _latest_data_req(events: list, *, exclude: set[str]) -> str | None:
    """从 turn_suspended 事件取最新一条 reason==data 的 request_id（排除已见集合）。"""
    found: str | None = None
    for ev in events:
        if ev.msg.kind != "turn_suspended":
            continue
        for p in ev.msg.data.get("pending") or []:
            if p.get("reason") == "data" and p["request_id"] not in exclude:
                found = p["request_id"]
    return found


async def _active_record(pool, tid: str) -> SuspensionRecord:
    """读子 thread 最新一条 suspension item 为 SuspensionRecord。"""
    items = [it async for it in await pool.store.load_thread(tid)]
    recs = [it for it in items if it.kind == "suspension"]
    assert recs, f"thread {tid} 无 suspension 记录"
    return SuspensionRecord.from_item(recs[-1])


async def _gated_danger_tool():
    """ask 门控的 danger 工具（parallel_safe=True → 同 turn 多 call 批量挂起出多 pending）。

    透传 ctx.call_id 进 PermissionRequest.metadata，使 SuspendingPrompter 把
    related_call_id 填进 PendingRequest，供 resume 定位被挂起 tool + 二次放行回填。
    """
    from taifeng.permission.types import PermissionRequest
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    async def handler(args, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        req = PermissionRequest.for_tool_call(
            "danger", args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        return ToolResult.ok(f"danger executed for {ctx.call_id}")

    return ToolSpec(
        name="danger", description="需审批的危险工具（边界测试用）",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler, parallel_safe=True,
    )


def _ask_policy():
    """default ask + 放行 skill_dispatch（spawn 派发不挂起）；danger 走 SuspendingPrompter。"""
    from taifeng.permission.types import (
        PermissionPolicy,
        PermissionRule,
        SuspendingPrompter,
    )

    return PermissionPolicy(
        default_mode="ask",
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        prompter=SuspendingPrompter(),
    )


# ───────────────── 1. 直接挂起 · 部分核销 → 保持 suspended ─────────────────

@pytest.mark.asyncio
async def test_spawn_direct_partial_resolution_keeps_suspended(tmp_path, threads_dir):
    """spawn 子 thread 单 turn danger×2 并发挂起 → 只 resolve 1 个 → 部分核销
    （SuspensionPartiallyResolved + 句柄保持 suspended、不续跑）；补齐另 1 个 → done。"""
    skills = _write_skills(tmp_path / "dp", {
        "host": _skill("host", entry=True, children=["dmulti"]),
        "dmulti": _skill("dmulti", tools=["danger"]),
    })
    client = RoutingSimClient(routes={
        "DMULTI_MARK": [
            SimTurn(text="两个危险操作", tool_calls=[
                {"id": "call_a", "name": "danger", "arguments": "{}"},
                {"id": "call_b", "name": "danger", "arguments": "{}"}]),
            SimTurn(text="DIRECT_PARTIAL_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[await _gated_danger_tool()],
        permission_policy=_ask_policy())
    engine = await pool.get_or_create(session_id="dp", entry_skill_id="host")
    events, task = _collect_events(engine)
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="dmulti", args={}, reason="多 pending")
    hid, ctid = sp["handle_id"], sp["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), "未挂起"

    # 子 thread 一条 record 含两条 PERMISSION pending（call_a / call_b）
    rec = await _active_record(pool, ctid)
    assert len(rec.pending) == 2, f"应两 pending 并发挂起，实得 {len(rec.pending)}"
    req_ids = sorted(rec.request_ids())

    # 只 resolve 第一个 → 部分核销，句柄保持 suspended
    await engine.submit(Resume(thread_id=ctid, resolutions={req_ids[0]: {"granted": True}}))
    assert await _wait(
        lambda: any(ev.msg.kind == "suspension_partially_resolved"
                    and ev.msg.data.get("thread_id") == ctid for ev in events)), \
        "子集 resume 应 emit SuspensionPartiallyResolved"
    partial = next(ev for ev in events
                   if ev.msg.kind == "suspension_partially_resolved"
                   and ev.msg.data.get("thread_id") == ctid)
    assert partial.msg.data["remaining_request_ids"] == [req_ids[1]], "剩余应恰为未提交的那个"
    # 短暂等待确认句柄并未因部分核销而续跑/终态
    await asyncio.sleep(0.1)
    assert engine.spawn_status([hid])[hid]["status"] == "suspended", \
        "部分核销不应令句柄离开 suspended"

    # 补齐另一个 → 全量达成 → 续跑至 done
    await engine.submit(Resume(thread_id=ctid, resolutions={req_ids[1]: {"granted": True}}))
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), "补齐后应续跑至 done"
    assert engine.spawn_status([hid])[hid]["result"] == "DIRECT_PARTIAL_DONE"

    task.cancel()
    await pool.close()


# ───────────────── 2. 在飞守卫 · 同 record 重入 → resolve_in_flight ─────────────────

@pytest.mark.asyncio
async def test_spawn_direct_resume_in_flight_rejected(tmp_path, threads_dir):
    """同一挂起 record 已在结算中（_resolving_records 命中）→ 再次 resume_spawn 应
    emit SuspensionResolveRejected(resolve_in_flight)、不重复结算、句柄保持 suspended。"""
    skills = _write_skills(tmp_path / "if", {
        "host": _skill("host", entry=True, children=["dask"]),
        "dask": _skill("dask", tools=["request_user_input"]),
    })
    client = RoutingSimClient(routes={
        "DASK_MARK": [
            SimTurn(text="补问", tool_calls=[
                {"id": "q", "name": "request_user_input", "arguments": '{"prompt": "Q"}'}]),
            SimTurn(text="IN_FLIGHT_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="if", entry_skill_id="host")
    events, task = _collect_events(engine)
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="dask", args={}, reason="在飞守卫")
    hid, ctid = sp["handle_id"], sp["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), "未挂起"

    rec = await _active_record(pool, ctid)
    handle = engine._spawn._spawn_handles.get(hid)  # noqa: SLF001

    # 模拟「该 record 已在结算中」：手动占住在飞集合，再直接驱动 resume_spawn。
    engine._resolving_records.add(rec.record_id)  # noqa: SLF001
    sub = Submission(
        id="inflight-sub",
        op=Resume(thread_id=ctid, resolutions={rec.pending[0].request_id: {"answer": "x"}}))
    try:
        await engine._spawn._resume.resume_spawn(sub, handle)  # noqa: SLF001
    finally:
        engine._resolving_records.discard(rec.record_id)  # noqa: SLF001

    # 轮询等订阅者协程把 emit 的事件 drain 进列表（直接驱动后事件传播是异步的）
    def _rej() -> list:
        return [ev for ev in events
                if ev.msg.kind == "suspension_resolve_rejected"
                and ev.submission_id == "inflight-sub"]

    assert await _wait(lambda: bool(_rej())), "在飞重入应 emit SuspensionResolveRejected"
    rejected = _rej()
    assert rejected[0].msg.data["reason"] == "resolve_in_flight"
    assert rejected[0].msg.data["record_id"] == rec.record_id
    # 句柄未被在飞重入改动，仍 suspended（可被真实那一次结算续跑）
    assert engine.spawn_status([hid])[hid]["status"] == "suspended"

    task.cancel()
    await pool.close()


# ───────────────── 3. 无活跃挂起 → no_active_suspension ─────────────────

@pytest.mark.asyncio
async def test_spawn_direct_resume_no_active_suspension(tmp_path, threads_dir):
    """句柄标 suspended 但子 thread 已无活跃挂起（已被核销）→ resume_spawn 应
    emit SuspensionResolveRejected(no_active_suspension)、不崩溃。"""
    skills = _write_skills(tmp_path / "na", {
        "host": _skill("host", entry=True, children=["dask"]),
        "dask": _skill("dask", tools=["request_user_input"]),
    })
    client = RoutingSimClient(routes={
        "DASK_MARK": [
            SimTurn(text="补问", tool_calls=[
                {"id": "q", "name": "request_user_input", "arguments": '{"prompt": "Q"}'}]),
            SimTurn(text="NO_ACTIVE_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="na", entry_skill_id="host")
    events, task = _collect_events(engine)
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="dask", args={}, reason="无活跃挂起")
    hid, ctid = sp["handle_id"], sp["child_thread_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), "未挂起"

    # 真实核销至 done（子 thread 落 resolved marker，活跃挂起消失）
    req = _latest_data_req(events, exclude=set())
    assert req is not None, "未观测 DATA 挂起"
    await engine.submit(Resume(thread_id=ctid, resolutions={req: {"answer": "a"}}))
    assert await _wait(lambda: engine.spawn_status([hid])[hid]["status"] == "done"), "未续跑至 done"

    # 把句柄强制改回 suspended，模拟「句柄标 suspended 但线上已无活跃挂起」竞态。
    engine._spawn._spawn_handles.set_result(hid, status="suspended", result=None)  # noqa: SLF001
    handle = engine._spawn._spawn_handles.get(hid)  # noqa: SLF001
    sub = Submission(
        id="noactive-sub",
        op=Resume(thread_id=ctid, resolutions={"whatever": {"answer": "x"}}))
    await engine._spawn._resume.resume_spawn(sub, handle)  # noqa: SLF001

    # 轮询等订阅者协程把 emit 的事件 drain 进列表（直接驱动后事件传播是异步的）
    def _rej() -> list:
        return [ev for ev in events
                if ev.msg.kind == "suspension_resolve_rejected"
                and ev.submission_id == "noactive-sub"]

    assert await _wait(lambda: bool(_rej())), "应 emit SuspensionResolveRejected"
    rejected = _rej()
    assert rejected[0].msg.data["reason"] == "no_active_suspension"
    assert rejected[0].msg.data["record_id"] is None

    task.cancel()
    await pool.close()


# ───────────────── 4. 多轮错峰 DATA HITL（直接路径）→ done ─────────────────

@pytest.mark.asyncio
async def test_spawn_direct_multi_round_staggered_hitl(tmp_path, threads_dir):
    """spawn 子 thread 自身连续两轮 request_user_input 挂起 → 逐轮 Resume
    （_finalize_spawn 每轮重标 suspended，可再 resume）→ 末轮 done。"""
    skills = _write_skills(tmp_path / "mr", {
        "host": _skill("host", entry=True, children=["d2"]),
        "d2": _skill("d2", tools=["request_user_input"]),
    })
    client = RoutingSimClient(routes={
        "D2_MARK": [
            SimTurn(text="第一次补问", tool_calls=[
                {"id": "q1", "name": "request_user_input", "arguments": '{"prompt": "Q1"}'}]),
            SimTurn(text="第二次补问", tool_calls=[
                {"id": "q2", "name": "request_user_input", "arguments": '{"prompt": "Q2"}'}]),
            SimTurn(text="MULTI_ROUND_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="mr", entry_skill_id="host")
    events, task = _collect_events(engine)
    await asyncio.sleep(0)

    sp = await engine.spawn_skill(skill_id="d2", args={}, reason="多轮错峰")
    hid, ctid = sp["handle_id"], sp["child_thread_id"]

    seen: set[str] = set()

    # 第 1 轮挂起
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended"), "第1轮未挂起"
    r1 = _latest_data_req(events, exclude=seen)
    assert r1 is not None, "未观测第1轮 DATA 挂起"
    seen.add(r1)
    await engine.submit(Resume(thread_id=ctid, resolutions={r1: {"answer": "a1"}}))

    # 第 2 轮挂起（_finalize_spawn 重标 suspended → 可再 resume）
    assert await _wait(
        lambda: _latest_data_req(events, exclude=seen) is not None), "第2轮未再次挂起"
    r2 = _latest_data_req(events, exclude=seen)
    assert r2 != r1, "第2轮 request_id 应与第1轮不同"
    seen.add(r2)
    await engine.submit(Resume(thread_id=ctid, resolutions={r2: {"answer": "a2"}}))

    # 末轮 done
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "done"), "多轮错峰后未抵 done"
    assert engine.spawn_status([hid])[hid]["result"] == "MULTI_ROUND_DONE"

    task.cancel()
    await pool.close()
