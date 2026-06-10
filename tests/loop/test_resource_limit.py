"""K2：会话级 token 上限强制（OOM-killer）—— 转内中止 + 跨 turn 拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import user_message
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.builtins import make_read_skill_tool
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


def test_session_limit_exceeded_helper(skills_dir: Path) -> None:
    import anyio

    reg = anyio.run(lambda: FilesystemSkillRegistry.load(skills_dir))
    entry = reg.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: object) -> None:
        return None

    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=SimClient(turns=[]), tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(), compressors=None, dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(), thread_id="t", submission_id="s",
        emit=_emit, cancel=CancellationToken(name="t"),
        session_tokens_used=80, max_session_tokens=100,
    )
    assert runner._session_limit_exceeded() is False  # 80 < 100  # noqa: SLF001
    runner.total_usage = TokenUsage(input_tokens=30, total_tokens=30)
    assert runner._session_limit_exceeded() is True  # 80+30 >= 100  # noqa: SLF001
    runner.max_session_tokens = None
    assert runner._session_limit_exceeded() is False  # 未配置→不强制  # noqa: SLF001


@pytest.mark.asyncio
async def test_turn_aborts_when_token_ceiling_hit_with_pending_work(
    skills_dir: Path,
) -> None:
    """超限且仍有 tool call → 中止本 turn（不再采样下一轮）。"""
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    # 一个带 tool_call 的 turn，usage 直接超 100；read_skill 空参→工具错误结果（不影响）
    client = SimClient(turns=[
        SimTurn(
            text="", tool_calls=[{"id": "c1", "name": "read_skill", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=200, total_tokens=200),
        ),
        SimTurn(text="should-not-reach"),
    ])
    events: list = []

    async def _emit(ev) -> None:  # noqa: ANN001
        events.append(ev.msg)

    registry = ToolRegistry()
    registry.register(make_read_skill_tool())  # 脚本要吐 read_skill,必须真注册进请求 tools
    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        history_buffer=[user_message("go", thread_id="t")],
        model_client=client, tool_runtime=ToolCallRuntime(registry),
        store=_FakeStore(), compressors=None, dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(), thread_id="t", submission_id="s",
        emit=_emit, cancel=CancellationToken(name="t"),
        session_tokens_used=0, max_session_tokens=100,
    )
    outcome = await runner.run()
    assert outcome.end_reason == "resource_limit_exceeded"
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert len(rl) == 1
    assert rl[0].data["scope"] == "turn_aborted"
    assert client._idx == 1  # 第二个 turn 未被采样  # noqa: SLF001


@pytest.mark.asyncio
async def test_engine_refuses_new_turn_after_session_limit(
    skills_dir: Path, threads_dir: Path
) -> None:
    """跨 turn：turn1 耗尽预算 → turn2 在 pre-turn 守卫被拒。"""
    client = SimClient(turns=[
        SimTurn(text="一", usage=TokenUsage(input_tokens=200, total_tokens=200)),
        SimTurn(text="二", usage=TokenUsage(input_tokens=10, total_tokens=10)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], max_session_tokens=100,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    sub1 = await engine.submit(taifeng.UserMessage(text="hi"))
    async for ev in engine.subscribe(sub1):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    events: list = []
    sub2 = await engine.submit(taifeng.UserMessage(text="hi2"))
    async for ev in engine.subscribe(sub2):
        events.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    failed = next(m for m in events if m.kind == "turn_failed")
    assert failed.data["kind"] == "resource_limit_exceeded"
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert rl and rl[0].data["scope"] == "turn_refused"

    await pool.close()


# ---------------------------------------------------------------------------
# resource-limit-retry-semantics:K2 retry 增额 / limit 类失败进 policy
# ---------------------------------------------------------------------------

async def _drain_until(engine, events: list, pred, max_wait: float = 8.0) -> None:
    """订阅全量事件直到谓词命中(事件累积进 events)。"""
    import asyncio

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if pred(events):
                return

    await asyncio.wait_for(watch(), timeout=max_wait)


@pytest.mark.asyncio
async def test_k2_suspend_retry_requires_extend_and_unblocks(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """K2 触顶挂起(SuspendByDefault)闭环:① scope 如实报 turn_suspended;
    ② 裸 retry 被拒(k2_retry_requires_extend_tokens——旧实现 retry 永久再触顶);
    ③ retry+extend_tokens 抬顶后真实续跑完成,不再立即触顶。"""
    from taifeng.loop.submission import Resume

    client = SimClient(turns=[
        SimTurn(text="", tool_calls=[{"id": "c1", "name": "read_skill", "arguments": "{}"}],
                 usage=TokenUsage(input_tokens=200, total_tokens=200)),
        SimTurn(text="K2_DONE", usage=TokenUsage(input_tokens=10, total_tokens=10)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], max_session_tokens=100,
        failure_policy=taifeng.SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="k2x", entry_skill_id="code-reviewer")
    events: list = []
    await engine.submit(taifeng.UserMessage(text="go"))
    await _drain_until(engine, events,
                       lambda s: any(m.kind == "turn_suspended" for m in s))

    # ① scope 如实
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert rl and rl[0].data["scope"] == "turn_suspended", \
        f"挂起时 scope 不得谎报 turn_aborted: {[m.data for m in rl]}"
    susp = next(m for m in events if m.kind == "turn_suspended")
    req_id = susp.data["pending"][0]["request_id"]

    # ② 裸 retry = 无效裁决(必然立即再触顶),显式拒绝
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={req_id: {"action": "retry"}}))
    await _drain_until(engine, events, lambda s: any(
        m.kind == "suspension_resolve_rejected" for m in s))
    rej = next(m for m in events if m.kind == "suspension_resolve_rejected")
    assert "k2_retry_requires_extend_tokens" in rej.data["reason"]

    # ③ retry + 增额 → 抬顶续跑至完成
    await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"action": "retry", "extend_tokens": 500}}))
    await _drain_until(engine, events, lambda s: any(
        m.kind == "turn_completed" and m.data.get("is_root") for m in s))

    await pool.close()


@pytest.mark.asyncio
async def test_k2_turn_refused_goes_through_policy(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """K2 引擎级拒新 turn 进 policy(limit 类失败一律可 retry 的挂起):
    SuspendByDefault 下第二条 UserMessage 落 RESOURCE_LIMIT 挂起而非 TurnFailed;
    retry+extend 后该 turn 正常执行(user_message 已入史,续跑即跑)。"""
    from taifeng.loop.submission import Resume

    client = SimClient(turns=[
        SimTurn(text="一", usage=TokenUsage(input_tokens=200, total_tokens=200)),
        SimTurn(text="REFUSED_THEN_DONE",
                 usage=TokenUsage(input_tokens=10, total_tokens=10)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], max_session_tokens=100,
        failure_policy=taifeng.SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="k2r", entry_skill_id="code-reviewer")
    events: list = []
    await engine.submit(taifeng.UserMessage(text="第一问"))
    await _drain_until(engine, events, lambda s: any(
        m.kind == "turn_completed" and m.data.get("is_root") for m in s))

    await engine.submit(taifeng.UserMessage(text="第二问"))
    await _drain_until(engine, events,
                       lambda s: any(m.kind == "turn_suspended" for m in s))
    assert not any(m.kind == "turn_failed" for m in events), \
        "SuspendByDefault 下 turn_refused 不得直落终态"
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert rl[-1].data["scope"] == "turn_suspended"
    susp = next(m for m in events if m.kind == "turn_suspended")
    req_id = susp.data["pending"][0]["request_id"]

    await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={req_id: {"action": "retry", "extend_tokens": 500}}))
    await _drain_until(engine, events, lambda s: sum(
        1 for m in s if m.kind == "turn_completed" and m.data.get("is_root")) >= 2)
    await pool.close()


@pytest.mark.asyncio
async def test_request_too_large_precheck_goes_through_policy(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """RequestTooLargeError 预检进 policy:SuspendByDefault → SYSTEM_RETRY 挂起
    (业务压缩/改参后可 retry);Conservative(默认)→ 原样 TurnFailed 零变化。"""
    # SuspendByDefault → 挂起
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=[SimTurn(text="x")]), compressors=[],
        budget=ContextBudget(max_request_bytes=10),
        failure_policy=taifeng.SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="rtl1", entry_skill_id="code-reviewer")
    events: list = []
    await engine.submit(taifeng.UserMessage(text="超长输入" * 10))
    await _drain_until(engine, events,
                       lambda s: any(m.kind == "turn_suspended" for m in s))
    susp = next(m for m in events if m.kind == "turn_suspended")
    assert susp.data["pending"][0]["reason"] == "system_retry"
    assert susp.data["pending"][0]["detail"]["kind"] == "RequestTooLargeError"
    await pool.close()

    # Conservative → 终态零变化
    pool2 = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=[SimTurn(text="x")]), compressors=[],
        budget=ContextBudget(max_request_bytes=10),
    )
    engine2 = await pool2.get_or_create(session_id="rtl2", entry_skill_id="code-reviewer")
    events2: list = []
    await engine2.submit(taifeng.UserMessage(text="超长输入" * 10))
    await _drain_until(engine2, events2,
                       lambda s: any(m.kind == "turn_failed" for m in s))
    assert not any(m.kind == "turn_suspended" for m in events2)
    await pool2.close()
