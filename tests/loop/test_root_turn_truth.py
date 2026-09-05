"""根 turn 真相回归（wave2a）：热 == 冷、注入不丢、终结信号完整、根 turn 串行。

四条用例来自 2026-09-03 审查的 SimClient 复现（scratchpad repro_engine.py /
repro_sub_hang.py），全部走真实 EnginePool，不 mock 内核。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import (
    CancelTurn,
    InjectSystemMessage,
    InjectUserInput,
    Shutdown,
)
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = "code-reviewer"


async def _pool(skills_dir: Path, threads_dir: Path, client: SimClient) -> taifeng.EnginePool:
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )


def _shape(items: list[Any]) -> list[tuple[str, str]]:
    return [(it.kind, str(it.payload.get("text", ""))[:24]) for it in items]


async def _cold(pool: taifeng.EnginePool, thread_id: str) -> list[Any]:
    return [it async for it in await pool.store.load_thread(thread_id)]


async def _wait_terminal(engine: Any, sub_id: str) -> list[Any]:
    """收集事件直到该 submission 终态。"""
    seen: list[Any] = []
    async for ev in engine.subscribe(sub_id):
        seen.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break
    return seen


# ---------------------------------------------------------------------------
# a) 在飞 InjectSystemMessage：热 == 冷
# ---------------------------------------------------------------------------


async def test_inflight_system_injection_hot_equals_cold(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = SimClient(turns=[SimTurn(text="slow answer", delay_seconds=0.4)] * 2)
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub_id = await engine.submit(taifeng.UserMessage(text="hello"))
        await asyncio.sleep(0.1)  # turn 已在采样中
        await engine.submit(InjectSystemMessage(text="SYSTEM-NOTE-DURING-TURN"))
        await _wait_terminal(engine, sub_id)
        await asyncio.sleep(0.1)

        hot = _shape(engine.history_snapshot())
        cold = _shape(await _cold(pool, engine.thread_id))
        assert hot == cold, f"hot={hot}\ncold={cold}"
        kinds = [k for k, _ in hot]
        # 单迭代 turn：注入在 turn 收尾的迭代边界并入 → 位于 assistant 之后；
        # 关键不变量是热 == 冷（位置 = 消费时刻），而非"一定在 assistant 之前"
        assert kinds == ["user_message", "assistant_message", "system_injection"]
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# b) InjectUserInput 后 CancelTurn：注入不丢，事件 delivered:false
# ---------------------------------------------------------------------------


async def test_inject_then_cancel_keeps_user_input(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = SimClient(turns=[SimTurn(text="slow answer", delay_seconds=0.6)] * 2)
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub_id = await engine.submit(taifeng.UserMessage(text="hello"))
        await asyncio.sleep(0.1)
        await engine.submit(InjectUserInput(submission_id=sub_id, text="STEER-TEXT"))
        await asyncio.sleep(0.05)
        await engine.submit(CancelTurn(submission_id=sub_id))
        seen = await _wait_terminal(engine, sub_id)
        await asyncio.sleep(0.1)

        hot = _shape(engine.history_snapshot())
        cold = _shape(await _cold(pool, engine.thread_id))
        assert hot == cold, f"hot={hot}\ncold={cold}"
        assert ("user_message", "STEER-TEXT") in hot

        injected = [m for m in seen if m.kind == "user_input_injected"]
        assert injected, "must emit user_input_injected for the residual drain"
        assert injected[-1].data["delivered"] is False
        assert injected[-1].data["reason"] == "turn_ended"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# c) Shutdown 后的过滤订阅必须收到终结事件
# ---------------------------------------------------------------------------


async def test_subscriber_after_shutdown_receives_terminal_event(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = SimClient(turns=[SimTurn(text="x")] * 2)
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        await engine.submit(Shutdown())
        sub_id = await engine.submit(taifeng.UserMessage(text="hello"))

        async def _first_kind() -> tuple[str, dict]:
            async for ev in engine.subscribe(sub_id):
                return ev.msg.kind, dict(ev.msg.data)
            return "iterator-ended", {}

        kind, data = await asyncio.wait_for(_first_kind(), timeout=3.0)
        assert kind == "turn_failed"
        assert data["kind"] == "engine_shutdown"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# d) 两条 UserMessage 连发：串行、顺序、热 == 冷
# ---------------------------------------------------------------------------


async def test_two_user_messages_run_serially_in_order(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = SimClient(turns=[
        SimTurn(text="answer-A", delay_seconds=0.4),
        SimTurn(text="answer-B"),
    ])
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)

        # 全程用 firehose 收，且在**任何 submit 之前**就建立订阅：
        # 原写法是 submit 完再 subscribe(sub_id)，过滤订阅是 live-only，turn 一快
        # 就整段错过（ADR 0031 的终态补投只补终结，补不回 turn_started）；
        # 而 sleep(0.05) 又被当成「B 落进 A 在飞窗口」的同步手段。两个赌注叠在一起，
        # 负载下就是本用例历史上间歇报 TimeoutError 的根因。
        events: list[tuple[str, str]] = []

        async def _firehose() -> None:
            async for ev in engine.subscribe_all():
                events.append((ev.submission_id, ev.msg.kind))
                if ev.msg.kind == "shutdown":
                    return

        collector = asyncio.create_task(_firehose())
        await wait_for_condition(
            lambda: bool(engine._all_subs),  # noqa: SLF001 —— 订阅真的登记上了才发提交
            message="firehose 订阅未能登记",
        )

        sub_a = await engine.submit(taifeng.UserMessage(text="A"))
        # 前提是「B 提交时 A 正在飞」——等 A 真的起飞，而不是睡一个估计值
        await wait_for_condition(
            lambda: (sub_a, "turn_started") in events,
            message="A 的 turn_started 未出现",
        )
        sub_b = await engine.submit(taifeng.UserMessage(text="B"))

        await wait_for_condition(
            lambda: any(
                (sid, kind) in events
                for sid in (sub_b,)
                for kind in ("turn_completed", "turn_failed")
            ),
            message="B 未走到终态",
        )
        collector.cancel()

        # B 的 turn_started 必须晚于 A 的 turn_completed
        order = [(sid, k) for sid, k in events if k in ("turn_started", "turn_completed")]
        assert order.index((sub_a, "turn_completed")) < order.index((sub_b, "turn_started"))
        # 排队事件的完整断言在 test_root_turn_gate.py

        hot = _shape(engine.history_snapshot())
        cold = _shape(await _cold(pool, engine.thread_id))
        assert hot == cold
        assert hot == [
            ("user_message", "A"), ("assistant_message", "answer-A"),
            ("user_message", "B"), ("assistant_message", "answer-B"),
        ]
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# e) operation 崩溃：终结事件 + 无幽灵 pending
# ---------------------------------------------------------------------------


async def test_crashed_operation_emits_terminal_and_clears_pending(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """operation 抛未捕获异常 → 订阅者收到 turn_failed{kind=<异常类名>}，introspect 无幽灵。"""
    client = SimClient(turns=[SimTurn(text="x")] * 2)
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)

        async def _boom() -> None:
            raise RuntimeError("resumed_tool_call_not_found")

        sub_id = "sub-crash"
        engine._start_operation(_boom(), name="boom", submission_id=sub_id)  # noqa: SLF001

        async def _first() -> tuple[str, dict]:
            async for ev in engine.subscribe(sub_id):
                return ev.msg.kind, dict(ev.msg.data)
            return "iterator-ended", {}

        kind, data = await asyncio.wait_for(_first(), timeout=3.0)
        assert kind == "turn_failed"
        assert data["kind"] == "RuntimeError"
        assert "resumed_tool_call_not_found" in data["error"]
        assert sub_id not in engine.introspect()["pending_submissions"]
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# f) 晚到的过滤订阅（engine 已收敛）立即得到合成终结
# ---------------------------------------------------------------------------


async def test_late_subscriber_after_engine_closed_gets_terminal(
    skills_dir: Path, threads_dir: Path,
) -> None:
    client = SimClient(turns=[SimTurn(text="x")] * 2)
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        await engine.submit(Shutdown())
        await asyncio.sleep(0.3)  # 让 run() 收敛完毕

        async def _first() -> str:
            async for ev in engine.subscribe("never-submitted"):
                return ev.msg.kind
            return "iterator-ended"

        assert await asyncio.wait_for(_first(), timeout=3.0) == "turn_failed"
    finally:
        await pool.close()
