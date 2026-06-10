"""suspension-ttl 单测 —— 数据契约校验 / expires_at 派生 / resolver 到期裁决 /
engine 定时器(热武装 / 先核销者胜 / 冷重武装)。

对应 openspec change ``suspension-ttl-auto-adjudication``。
"""
from __future__ import annotations

import asyncio
import time

import pytest

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.loop.submission import Resume
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.resolver import EXPIRE_SENTINEL, SuspensionResolver
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool


def _rec(*reqs, created_at: int = 1000) -> SuspensionRecord:
    return SuspensionRecord(
        record_id="sr", thread_id="t", submission_id="s",
        turn_index=1, pending=tuple(reqs), created_at=created_at,
    )


# ---------- 构造期校验 ----------

def test_pending_validation_rejects_nonpositive_ttl():
    """ttl_seconds ≤ 0(含 -1 哨兵)构造期抛 ValueError。"""
    for bad in (0, -1, -100):
        with pytest.raises(ValueError, match="ttl_seconds"):
            PendingRequest(request_id="r", reason=SuspendReason.DATA,
                           ttl_seconds=bad)


def test_pending_validation_rejects_retry_on_human_input():
    """DATA / FORM / PERMISSION / CHILD_SKILL 禁 on_expire='retry'。"""
    for reason in (SuspendReason.DATA, SuspendReason.FORM,
                   SuspendReason.PERMISSION, SuspendReason.CHILD_SKILL):
        with pytest.raises(ValueError, match="on_expire"):
            PendingRequest(request_id="r", reason=reason,
                           ttl_seconds=60, on_expire="retry")


def test_pending_retry_allowed_on_system_reasons():
    """SYSTEM_RETRY / RESOURCE_LIMIT 可声明 on_expire='retry'。"""
    for reason in (SuspendReason.SYSTEM_RETRY, SuspendReason.RESOURCE_LIMIT):
        p = PendingRequest(request_id="r", reason=reason,
                           ttl_seconds=60, on_expire="retry")
        assert p.on_expire == "retry"


def test_default_no_ttl_zero_change():
    """默认不声明 ttl → None 永不过期,record 无到期时刻(零行为变化)。"""
    rec = _rec(PendingRequest(request_id="r", reason=SuspendReason.DATA))
    assert rec.expires_at is None


# ---------- expires_at 派生 + 序列化 round-trip ----------

def test_expires_at_takes_min_ttl():
    """record 到期时刻 = created_at + min(各 pending ttl);无 ttl 的不参与。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.DATA, ttl_seconds=300),
        PendingRequest(request_id="r2", reason=SuspendReason.SYSTEM_RETRY,
                       ttl_seconds=60, on_expire="retry"),
        PendingRequest(request_id="r3", reason=SuspendReason.FORM),  # 无 ttl
        created_at=1000,
    )
    assert rec.expires_at == 1060


def test_ttl_fields_roundtrip_via_item():
    """ttl_seconds / on_expire 随 to_item 落盘、from_item 还原;expires_at 冷热一致。"""
    rec = _rec(PendingRequest(
        request_id="r1", reason=SuspendReason.RESOURCE_LIMIT,
        ttl_seconds=120, on_expire="retry"), created_at=500)
    back = SuspensionRecord.from_item(rec.to_item())
    assert back.pending[0].ttl_seconds == 120
    assert back.pending[0].on_expire == "retry"
    assert back.expires_at == 620


def test_old_jsonl_without_ttl_fields_loads():
    """旧 JSONL(pending 无 ttl 字段)装载 → 默认 None/abort,永不过期(前向兼容)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.DATA))
    item = rec.to_item()
    # 模拟旧记录:剔除新字段
    for d in item.payload["pending"]:
        d.pop("ttl_seconds", None)
        d.pop("on_expire", None)
    back = SuspensionRecord.from_item(item)
    assert back.pending[0].ttl_seconds is None
    assert back.pending[0].on_expire == "abort"
    assert back.expires_at is None


# ---------- resolver 到期裁决(EXPIRE_SENTINEL) ----------

def _expire_all(rec: SuspensionRecord) -> dict:
    return {rid: {EXPIRE_SENTINEL: True} for rid in rec.request_ids()}


def test_expire_system_retry_with_retry():
    """SYSTEM_RETRY 到期 on_expire=retry → 不 abort(自动续跑,重建即重采样)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is False  # retry:不 abort 即续跑(无 resample 位,重建续跑天然重采样)
    assert plan.abort is False


def test_expire_resource_limit_with_retry_not_abort():
    """RESOURCE_LIMIT 到期 retry → 不 abort(重建续跑即继续循环)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.RESOURCE_LIMIT,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is False


def test_expire_data_form_permission_abort_with_gap_fill():
    """人类输入类到期 → 悬空 fc 回填 suspension_expired error + 整体 abort。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.DATA,
                       related_call_id="ca", ttl_seconds=60),
        PendingRequest(request_id="r2", reason=SuspendReason.PERMISSION,
                       related_call_id="cb", ttl_seconds=60),
    )
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.deny_outputs["ca"] == "ttl_reached"
    assert plan.deny_outputs["cb"] == "ttl_reached"  # 渲染前缀由 engine 按 reason 决定
    assert plan.abort is True
    assert plan.execute_tool_call_ids == []


def test_expire_mixed_record_abort_wins():
    """混合 record(retry 系统位 + 人类输入)到期 → abort 胜出(record 级一次性)。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                       ttl_seconds=60, on_expire="retry"),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM,
                       related_call_id="cf", ttl_seconds=60),
    )
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is True


def test_expire_system_abort_default():
    """SYSTEM_RETRY 到期默认 on_expire=abort → abort。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is True


# ============================================================
# engine 定时器:热武装到期 / 先核销者胜 / 内核挂起自动 retry / 冷重武装
# ============================================================

_ASK = """---
name: ask-skill
description: 问询入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [ask-skill-noop]
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 ASK_MARK
先问人再下结论。
"""

_NOOP = """---
name: ask-skill-noop
description: 占位子技能
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 1
---
# 占位 NOOP_MARK
"""


@pytest.fixture
def ask_skills(tmp_path):
    skills = tmp_path / "ask_skills"
    for sub, body in (("ask-skill", _ASK), ("ask-skill-noop", _NOOP)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


def _ask_turns() -> list[SimTurn]:
    return [
        SimTurn(text="提问", tool_calls=[
            {"id": "ask1", "name": "request_user_input",
             "arguments": '{"prompt": "请补充"}'}]),
        SimTurn(text="ASK_DONE"),
    ]


def _future_now():
    """注入的壁钟:真实时间 + 1 小时 → 任何 ≤3600s 的 ttl 装载即过期(立即触发)。"""
    return int(time.time()) + 3600


async def _collect_until(engine, pred, max_wait: float = 5.0):
    """订阅全量事件直到谓词命中;返回事件 kind 列表(诊断用)。"""
    seen: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            seen.append(ev.msg)
            if pred(seen):
                return

    await asyncio.wait_for(watch(), timeout=max_wait)
    return seen


async def test_expire_data_suspend_auto_aborts(ask_skills, threads_dir):
    """DATA 挂起到期(ttl=60,注入未来时钟 → 立即触发)→ suspension_expired →
    自动核销(gap 回填 suspension_expired)且不续跑新 turn。"""
    client = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-abort", entry_skill_id="ask-skill")
    await engine.submit(taifeng.UserMessage(text="开始"))
    seen = await _collect_until(
        engine,
        lambda s: any(m.kind == "suspension_resolved" for m in s))
    kinds = [m.kind for m in seen]
    assert "turn_suspended" in kinds
    assert "suspension_expired" in kinds, f"应有到期事件,实得 {kinds}"
    # abort:核销后不再起新 turn(turn_started 仅首个)
    await asyncio.sleep(0.1)
    assert kinds.count("turn_started") == 1
    # 落盘验证:悬空 fc 被回填 error output,record 已核销
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    fco = [it for it in items if it.kind == "function_call_output"
           and it.payload.get("call_id") == "ask1"]
    assert fco and "suspension_expired" in str(fco[0].payload.get("output"))
    await pool.close()


async def test_manual_resume_wins_over_timer(ask_skills, threads_dir):
    """ttl 未到期(真实时钟,ttl=3600)→ 人工 Resume 先核销 → 定时器被撤销,
    无 suspension_expired,turn 正常完成。"""
    client = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=3600)],
    )
    engine = await pool.get_or_create(session_id="ttl-win", entry_skill_id="ask-skill")
    sub_id = await engine.submit(taifeng.UserMessage(text="开始"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind == "turn_suspended":
            req_id = ev.msg.data["pending"][0]["request_id"]
            break
    assert engine._ttl_timers, "挂起后应武装定时器"  # noqa: SLF001
    resume_id = await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"answer": "ok"}}))
    async for ev in engine.subscribe(resume_id):
        if ev.msg.kind == "turn_completed":
            break
    await asyncio.sleep(0.05)
    assert not engine._ttl_timers, "人工核销后定时器应撤销"  # noqa: SLF001
    await pool.close()


async def test_kernel_system_retry_expire_auto_retries(ask_skills, threads_dir):
    """内核 SYSTEM_RETRY 挂起(限流重试耗尽)配 failure_suspend_ttl + on_expire=retry
    → 到期自动 resample 续跑至完成(无人值守自愈)。"""
    from taifeng.llm.errors import RateLimitError

    class _FlakyOnce(RoutingSimClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._raised = False

        def session(self, *, cancel, model=None):  # noqa: ANN001, ANN201
            outer = self
            inner = super().session(cancel=cancel, model=model)

            class _S:
                async def __aenter__(self):  # noqa: ANN204
                    return self

                async def __aexit__(self, *e):  # noqa: ANN002, ANN204
                    pass

                async def stream(self, request):  # noqa: ANN001, ANN201
                    if not outer._raised:
                        outer._raised = True
                        raise RateLimitError("rl")
                    async with inner as s:
                        async for ev in s.stream(request):
                            yield ev

            return _S()

    client = _FlakyOnce(routes={"ASK_MARK": [SimTurn(text="RECOVERED")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        failure_suspend_ttl_seconds=60,
        failure_suspend_on_expire="retry",
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-retry", entry_skill_id="ask-skill")
    await engine.submit(taifeng.UserMessage(text="开始"))
    seen = await _collect_until(
        engine,
        lambda s: any(m.kind == "turn_completed" and m.data.get("is_root")
                      for m in s))
    kinds = [m.kind for m in seen]
    assert "turn_suspended" in kinds
    assert "suspension_expired" in kinds
    done = [m for m in seen if m.kind == "turn_completed"][-1]
    assert done.data["end_reason"] == "completed"
    await pool.close()


async def test_cold_rearm_fires_expired_on_load(ask_skills, threads_dir):
    """进程死亡期间过期:pool#1 挂起(ttl=60)后关闭;pool#2 注入未来时钟装载同
    thread → run 启动冷重武装立即裁决(suspension_expired + 核销)。"""
    client = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool1 = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
    )
    engine1 = await pool1.get_or_create(session_id="ttl-cold", entry_skill_id="ask-skill")
    sub_id = await engine1.submit(taifeng.UserMessage(text="开始"))
    async for ev in engine1.subscribe(sub_id):
        if ev.msg.kind == "turn_suspended":
            break
    tid = engine1.thread_id
    await pool1.close()

    # pool#2:同 threads_dir 冷恢复;未来时钟 → 装载即过期
    client2 = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool2 = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client2,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine2 = await pool2.get_or_create(
        session_id="ttl-cold-2", entry_skill_id="ask-skill",
        resume_thread_id=tid)
    seen = await _collect_until(
        engine2,
        lambda s: any(m.kind == "suspension_resolved" for m in s))
    kinds = [m.kind for m in seen]
    assert "suspension_expired" in kinds, f"冷装载应触发到期,实得 {kinds}"
    await pool2.close()


# ============================================================
# spawn 链:挂起的 spawn 子 thread 到期 → abort 解除 barrier 占用 / retry 续跑
# ============================================================

_SPAWN_HOST = """---
name: ttl-host
description: 宿主
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [ttl-expert, ttl-fast]
max_call_depth: 3
---
# 宿主 TTL_HOST_MARK
派发专家。
"""

_SPAWN_EXPERT = """---
name: ttl-expert
description: 问询专家
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 专家 TTL_EXPERT_MARK
先问人再下结论。
"""

_SPAWN_FAST = """---
name: ttl-fast
description: 速诊专家
version: 1.0.0
type: atomic
---
# 速诊 TTL_FAST_MARK
直接给结论。
"""

_SPAWN_CONSULT = """---
name: ttl-consult
description: 聚合汇总
version: 1.0.0
type: atomic
---
# 汇总 TTL_CONSULT_MARK
综合各专家终态出报告。
"""


@pytest.fixture
def ttl_spawn_skills(tmp_path):
    skills = tmp_path / "ttl_spawn_skills"
    for sub, body in (
        ("ttl-host", _SPAWN_HOST), ("ttl-expert", _SPAWN_EXPERT),
        ("ttl-fast", _SPAWN_FAST), ("ttl-consult", _SPAWN_CONSULT),
    ):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return skills


async def _wait_status(engine, hid: str, want: str, tries: int = 200) -> bool:
    for _ in range(tries):
        if engine.spawn_status([hid])[hid]["status"] == want:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_spawn_suspend_expire_aborts_to_failed(ttl_spawn_skills, threads_dir):
    """挂起的 spawn 子任务到期(DATA,on_expire=abort)→ 句柄 error + SpawnFailed,
    且解除 barrier 占用(单句柄 barrier 在 abort 终态后触发,无人值守不死锁)。"""
    client = RoutingSimClient(routes={
        "TTL_EXPERT_MARK": [
            SimTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            SimTurn(text="不应被采样"),
        ],
        "TTL_CONSULT_MARK": [SimTurn(text="汇总综合 CONSULT_DONE")],
        "TTL_HOST_MARK": [SimTurn(text="host idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=ttl_spawn_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-sp", entry_skill_id="ttl-host")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    h = await engine.spawn_skill(skill_id="ttl-expert", args={}, reason="t")
    hid = h["handle_id"]
    # 登记单句柄 barrier:验证 abort 终态确实解除 barrier 占用(非名义覆盖)
    await engine.set_join_barrier([hid], then_skill_id="ttl-consult")
    # 挂起(SpawnSuspended)后定时器立即到期 → 自动 abort → 句柄 error
    assert await _wait_status(engine, hid, "error"), \
        f"到期 abort 后句柄应 error,实为 {engine.spawn_status([hid])[hid]}"
    kinds = [m.kind for m in events]
    assert "suspension_expired" in kinds
    failed = [m for m in events if m.kind == "spawn_failed"
              and m.data.get("handle_id") == hid]
    assert failed, "到期 abort 应 emit SpawnFailed"
    # abort 终态 → barrier 全终态重查 → 触发(修复前漏调重查,此处永等不到)
    for _ in range(200):
        if any(m.kind == "join_barrier_fired" for m in events):
            break
        await asyncio.sleep(0.02)
    assert any(m.kind == "join_barrier_fired" for m in events), \
        "abort 终态后单句柄 barrier 应触发"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def test_spawn_abort_recheck_fires_join_barrier(ttl_spawn_skills, threads_dir):
    """业务命中路径忠实重放(spawn-terminal-single-convergence):双子任务聚合,
    挂起子任务 TTL 到期 abort(spawn_failed)后 join-barrier 必须重查并触发聚合
    turn —— 修复前 resume_spawn 的 plan.abort 分支漏调 _check_barriers,
    barrier 永不触发、聚合挂死。"""
    import json

    client = RoutingSimClient(routes={
        "TTL_EXPERT_MARK": [
            SimTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
        ],
        "TTL_FAST_MARK": [SimTurn(text="速诊结论 FAST_DONE")],
        "TTL_CONSULT_MARK": [SimTurn(text="汇总综合 CONSULT_DONE")],
        "TTL_HOST_MARK": [SimTurn(text="host idle")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=ttl_spawn_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-jb", entry_skill_id="ttl-host")
    fired: dict = {"v": None}

    async def watch():
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "join_barrier_fired":
                fired["v"] = dict(ev.msg.data)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    a = (await engine.spawn_skill(
        skill_id="ttl-fast", args={}, reason="速诊"))["handle_id"]
    b = (await engine.spawn_skill(
        skill_id="ttl-expert", args={}, reason="问询"))["handle_id"]
    # 登记时 b 尚未终态 → 登记期的立即检查不触发,触发只能依赖 abort 终态后的重查
    await engine.set_join_barrier([a, b], then_skill_id="ttl-consult")
    # b 挂起后定时器立即到期 → 自动 abort → 句柄 error;a 速诊 → done
    assert await _wait_status(engine, b, "error"), \
        f"到期 abort 后句柄应 error,实为 {engine.spawn_status([b])[b]}"
    assert await _wait_status(engine, a, "done")
    # 核心断言:abort 终态使句柄集全终态 → barrier 重查触发(修复前永不触发)
    for _ in range(200):
        if fired["v"] is not None:
            break
        await asyncio.sleep(0.02)
    assert fired["v"] is not None, \
        "abort 终态后 join-barrier 应重查触发聚合 turn,实际未触发(聚合挂死)"
    # 聚合种子含两专家终态:a done / b error(失败专家不被静默丢弃)
    items = [it async for it in await pool.store.load_thread(
        fired["v"]["then_thread_id"])]
    payload = json.loads(items[0].payload["text"])
    assert payload[a]["status"] == "done"
    assert payload[b]["status"] == "error"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


# ---------------------------------------------------------------------------
# suspension-ttl-hardening:路由死角 / 生命周期 / 在飞竞态 / 边界收紧
# ---------------------------------------------------------------------------

_NEST_HOST = """---
name: nest-host
description: 宿主
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [nest-mid]
max_call_depth: 4
---
# 宿主 NEST_HOST_MARK
派发中层。
"""

_NEST_MID = """---
name: nest-mid
description: 中层
version: 1.0.0
type: composite
model: mock-model
child_skills: [nest-ask]
max_call_depth: 3
---
# 中层 NEST_MID_MARK
先派问询。
"""

_NEST_ASK = """---
name: nest-ask
description: 问询
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 NEST_ASK_MARK
先问人。
"""


async def test_spawn_nested_leaf_expire_routes_and_unblocks(tmp_path, threads_dir):
    """spawn 子的【嵌套】leaf 带 ttl 挂起到期(P0-4 路由死角修复):fire 时路由
    解析以 spawn 子 tid 提交 → 嵌套链 expire-abort leaf → 中层续跑 → 句柄落终态
    (barrier 解除)。修复前 Resume(leaf_tid) 不可路由 → no_active_suspension
    拒绝,句柄永滞 suspended、TTL 在该拓扑完全失效。"""
    skills = tmp_path / "nest_skills"
    for sub, body in (("nest-host", _NEST_HOST), ("nest-mid", _NEST_MID),
                      ("nest-ask", _NEST_ASK)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    client = RoutingSimClient(routes={
        "NEST_HOST_MARK": [SimTurn(text="host idle")],
        "NEST_MID_MARK": [
            SimTurn(text="派", tool_calls=[
                {"id": "m1", "name": "call_skill",
                 "arguments": '{"skill_id": "nest-ask", "reason": "go"}'}]),
            SimTurn(text="MID_DONE"),
        ],
        "NEST_ASK_MARK": [
            SimTurn(text="问", tool_calls=[
                {"id": "a1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            SimTurn(text="不应被采样"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-nest", entry_skill_id="nest-host")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)

    h = await engine.spawn_skill(skill_id="nest-mid", args={}, reason="t")
    hid = h["handle_id"]
    # leaf 到期 → 路由经 spawn 子 tid → expire-abort → 中层续跑 MID_DONE → done
    assert await _wait_status(engine, hid, "done"), \
        f"到期裁决应使句柄落终态,实为 {engine.spawn_status([hid])[hid]}"
    kinds = [m.kind for m in events]
    assert "suspension_expired" in kinds
    rejected = [m for m in events if m.kind == "suspension_resolve_rejected"]
    assert not rejected, f"到期裁决不得因路由死角被拒: {[m.data for m in rejected]}"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def test_root_cancel_clears_ttl_timers(ask_skills, threads_dir):
    """R4:root-cancel 退出路径同样清空全部 TTL 定时器(不只 Shutdown 分支)——
    孤儿定时器到期后会向无人消费的队列 submit。"""
    client = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        # 正常时钟 + 长 ttl:定时器武装后保持挂着(测清理而非触发)
        extra_tools=[make_request_user_input_tool(ttl_seconds=3600)],
    )
    engine = await pool.get_or_create(session_id="ttl-rc", entry_skill_id="ask-skill")
    await engine.submit(taifeng.UserMessage(text="开始"))
    await _collect_until(
        engine, lambda s: any(m.kind == "turn_suspended" for m in s))
    assert engine._ttl_timers, "挂起后应有武装中的定时器"  # noqa: SLF001
    # root-cancel(非 Shutdown Op)退出
    engine._root_cancel.cancel()  # noqa: SLF001
    for _ in range(100):
        if not engine._ttl_timers:  # noqa: SLF001
            break
        await asyncio.sleep(0.02)
    assert not engine._ttl_timers, "root-cancel 退出必须清空定时器(R4)"  # noqa: SLF001
    await pool.close()


async def test_inflight_guard_timer_noop_and_second_resume_rejected(
    ask_skills, threads_dir,
):
    """在飞守卫:① record 在飞时定时器 fire 为 no-op(不发 suspension_expired、
    不二次裁决);② 同 record 第二个 Resume 被 resolve_in_flight 拒绝;
    ③ 释放后人工 Resume 正常核销。"""
    client = RoutingSimClient(routes={"ASK_MARK": _ask_turns()})
    pool = await taifeng.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=3600)],
    )
    engine = await pool.get_or_create(session_id="ttl-if", entry_skill_id="ask-skill")
    await engine.submit(taifeng.UserMessage(text="开始"))
    seen = await _collect_until(
        engine, lambda s: any(m.kind == "turn_suspended" for m in s))
    susp = next(m for m in seen if m.kind == "turn_suspended")
    rid = susp.data["record_id"]
    req_id = susp.data["pending"][0]["request_id"]

    # ① 人为占位在飞 → 直接驱动到期任务体 → 必须 no-op
    engine._resolving_records.add(rid)  # noqa: SLF001
    events2: list = []
    task = asyncio.create_task(_watch_kinds(engine, events2))
    await asyncio.sleep(0)
    await engine._ttl_expire_after(0, engine.thread_id, rid)  # noqa: SLF001
    await asyncio.sleep(0.1)
    assert "suspension_expired" not in [m.kind for m in events2], \
        "在飞窗口 fire 必须 no-op(先核销者胜)"

    # ② 在飞期间的第二个 Resume 被显式拒绝
    from taifeng.loop.submission import Resume
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"answer": "x"}}))
    for _ in range(100):
        if any(m.kind == "suspension_resolve_rejected" for m in events2):
            break
        await asyncio.sleep(0.02)
    rejects = [m for m in events2 if m.kind == "suspension_resolve_rejected"]
    assert rejects and rejects[0].data["reason"] == "resolve_in_flight"

    # ③ 释放占位 → 人工 Resume 正常核销
    engine._resolving_records.discard(rid)  # noqa: SLF001
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"answer": "好"}}))
    for _ in range(150):
        if any(m.kind == "suspension_resolved" for m in events2):
            break
        await asyncio.sleep(0.02)
    assert any(m.kind == "suspension_resolved" for m in events2)

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def _watch_kinds(engine, sink: list) -> None:
    """后台收集事件直到 shutdown(in-flight 测试用)。"""
    async for ev in engine.subscribe_all():
        sink.append(ev.msg)
        if ev.msg.kind == "shutdown":
            return


async def test_engine_pool_ctor_rejects_nonpositive_failure_ttl(
    ask_skills, threads_dir,
):
    """构造期校验:failure_suspend_ttl_seconds ≤ 0 在 pool 构造点即 ValueError,
    不再延迟到首次护栏挂起被宽 except 吞为 turn_failed(报错点贴近配置点)。"""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="failure_suspend_ttl_seconds"):
        await taifeng.EnginePool.create(
            skills_dir=ask_skills, threads_dir=threads_dir,
            model_client=RoutingSimClient(routes={}), compressors=[],
            failure_suspend_ttl_seconds=-1,
        )


class _AlwaysFilteredClient(RoutingSimClient):
    """每次采样都抛确定性 ContentFilterError —— 测自动 retry 谱系熔断用。"""

    def __init__(self) -> None:
        super().__init__(routes={})
        self.sample_count = 0

    def session(self, *, cancel, model=None):  # noqa: ANN001, ANN201
        outer = self

        class _S:
            async def __aenter__(self):  # noqa: ANN204
                return self

            async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
                pass

            async def stream(self, request):  # noqa: ANN001, ANN201
                from taifeng.llm.errors import ContentFilterError
                outer.sample_count += 1
                raise ContentFilterError("always blocked")
                yield  # pragma: no cover - 使其成为 async generator

        return _S()


async def test_auto_retry_lineage_exhaustion_forces_abort(ask_skills, threads_dir):
    """自动 retry 谱系熔断(resource-limit-retry-semantics):确定性失败 +
    on_expire=retry + max_auto_retries=1 → 首次到期自动 retry(再失败再挂起,
    谱系计数 1),第二次到期判定达上限 → 强制 abort + 事件标注
    auto_retry_exhausted,终止无界自动循环。"""
    import taifeng as tf

    client = _AlwaysFilteredClient()
    pool = await tf.EnginePool.create(
        skills_dir=ask_skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        failure_policy=tf.SuspendByDefaultPolicy(),
        failure_suspend_ttl_seconds=60,
        failure_suspend_on_expire="retry",
        failure_suspend_max_auto_retries=1,
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-ex", entry_skill_id="ask-skill")
    await engine.submit(taifeng.UserMessage(text="开始"))

    # 等到带 exhausted 标注的到期事件(第二次 fire)
    seen = await _collect_until(
        engine,
        lambda s: any(m.kind == "suspension_expired"
                      and m.data.get("auto_retry_exhausted") for m in s),
        max_wait=8.0,
    )
    expired = [m for m in seen if m.kind == "suspension_expired"]
    assert len(expired) == 2, f"应恰两次到期(retry 一次 + 熔断一次),实得 {len(expired)}"
    assert "auto_retry_exhausted" not in expired[0].data, "首次到期应正常 retry"
    assert expired[1].data.get("auto_retry_exhausted") is True

    # 熔断后不再自动续跑:采样总数 = 2(首发 + 1 次自动 retry),不会无界增长
    await asyncio.sleep(0.3)
    assert client.sample_count == 2, \
        f"熔断后不得继续自动采样,实采 {client.sample_count} 次"

    await engine.submit(taifeng.loop.Shutdown())
    await pool.close()
