"""suspension-ttl 单测 —— 数据契约校验 / expires_at 派生 / resolver 到期裁决 /
engine 定时器(热武装 / 先核销者胜 / 冷重武装)。

对应 openspec change ``suspension-ttl-auto-adjudication``。
"""
from __future__ import annotations

import asyncio
import time

import pytest

import taifeng
from taifeng.llm.providers import MockTurn
from taifeng.llm.providers.mock import RoutingMockClient
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
    """SYSTEM_RETRY 到期 on_expire=retry → resample,不 abort(自动续跑)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.resample is True
    assert plan.abort is False


def test_expire_resource_limit_with_retry_no_resample():
    """RESOURCE_LIMIT 到期 retry → 不置 resample(重建续跑即继续循环)、不 abort。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.RESOURCE_LIMIT,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.resample is False
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
    assert plan.deny_outputs["ca"] == "suspension_expired"
    assert plan.deny_outputs["cb"] == "suspension_expired"
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
    """SYSTEM_RETRY 到期默认 on_expire=abort → abort 不 resample。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is True
    assert plan.resample is False


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


def _ask_turns() -> list[MockTurn]:
    return [
        MockTurn(text="提问", tool_calls=[
            {"id": "ask1", "name": "request_user_input",
             "arguments": '{"prompt": "请补充"}'}]),
        MockTurn(text="ASK_DONE"),
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
    client = RoutingMockClient(routes={"ASK_MARK": _ask_turns()})
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
    client = RoutingMockClient(routes={"ASK_MARK": _ask_turns()})
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

    class _FlakyOnce(RoutingMockClient):
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

    client = _FlakyOnce(routes={"ASK_MARK": [MockTurn(text="RECOVERED")]})
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
    client = RoutingMockClient(routes={"ASK_MARK": _ask_turns()})
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
    client2 = RoutingMockClient(routes={"ASK_MARK": _ask_turns()})
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
child_skills: [ttl-expert]
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


@pytest.fixture
def ttl_spawn_skills(tmp_path):
    skills = tmp_path / "ttl_spawn_skills"
    for sub, body in (("ttl-host", _SPAWN_HOST), ("ttl-expert", _SPAWN_EXPERT)):
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
    """挂起的 spawn 子专科到期(DATA,on_expire=abort)→ 句柄 error + SpawnFailed
    (解除 barrier 占用,无人值守不死锁)。"""
    client = RoutingMockClient(routes={
        "TTL_EXPERT_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            MockTurn(text="不应被采样"),
        ],
        "TTL_HOST_MARK": [MockTurn(text="host idle")],
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
    # 挂起(SpawnSuspended)后定时器立即到期 → 自动 abort → 句柄 error
    assert await _wait_status(engine, hid, "error"), \
        f"到期 abort 后句柄应 error,实为 {engine.spawn_status([hid])[hid]}"
    kinds = [m.kind for m in events]
    assert "suspension_expired" in kinds
    failed = [m for m in events if m.kind == "spawn_failed"
              and m.data.get("handle_id") == hid]
    assert failed, "到期 abort 应 emit SpawnFailed(barrier 可推进)"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
