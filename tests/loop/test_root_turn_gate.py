"""root gate（ADR 0029）：根 turn 排队串行、排队可观测、排队中可取消、gated Op 不阻塞 actor。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import CancelTurn, CompactNow, ThreadRollback

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = "code-reviewer"


async def _pool(skills_dir: Path, threads_dir: Path, client: SimClient) -> taifeng.EnginePool:
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )


class _Recorder:
    """subscribe_all 全量收集器（submit 前启动）。"""

    def __init__(self, engine: Any) -> None:
        self.events: list[Any] = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine: Any) -> None:
        async for ev in engine.subscribe_all():
            self.events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    def kinds(self, sub_id: str) -> list[str]:
        return [e.msg.kind for e in self.events if e.submission_id == sub_id]

    async def wait_kind(self, sub_id: str, kind: str, timeout: float = 5.0) -> Any:
        async def _poll() -> Any:
            while True:
                for e in self.events:
                    if e.submission_id == sub_id and e.msg.kind == kind:
                        return e
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout=timeout)


async def test_queued_submission_emits_submission_queued(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """B 在 A 在飞时提交 → submission_queued{waiting_on=A}，且 B 在 A 完成后才 turn_started。"""
    client = SimClient(turns=[
        SimTurn(text="A", delay_seconds=0.4), SimTurn(text="B"),
    ])
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        rec = _Recorder(engine)
        await asyncio.sleep(0)
        sub_a = await engine.submit(taifeng.UserMessage(text="A"))
        await asyncio.sleep(0.05)
        sub_b = await engine.submit(taifeng.UserMessage(text="B"))

        queued = await rec.wait_kind(sub_b, "submission_queued")
        assert queued.msg.data["waiting_on"] == sub_a
        await rec.wait_kind(sub_b, "turn_completed")

        seq_a_done = next(e.seq for e in rec.events
                          if e.submission_id == sub_a and e.msg.kind == "turn_completed")
        seq_b_start = next(e.seq for e in rec.events
                           if e.submission_id == sub_b and e.msg.kind == "turn_started")
        assert seq_a_done < seq_b_start
    finally:
        await pool.close()


async def test_cancel_turn_while_queued_terminates_without_running(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """排队中的 B 被 CancelTurn → turn_failed{kind=cancelled}，B 从未 turn_started，A 不受影响。"""
    client = SimClient(turns=[
        SimTurn(text="A", delay_seconds=0.5), SimTurn(text="B-should-not-run"),
    ])
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        rec = _Recorder(engine)
        await asyncio.sleep(0)
        sub_a = await engine.submit(taifeng.UserMessage(text="A"))
        await asyncio.sleep(0.05)
        sub_b = await engine.submit(taifeng.UserMessage(text="B"))
        await rec.wait_kind(sub_b, "submission_queued")
        await engine.submit(CancelTurn(submission_id=sub_b))

        failed = await rec.wait_kind(sub_b, "turn_failed")
        assert failed.msg.data["kind"] == "cancelled"
        assert "turn_started" not in rec.kinds(sub_b)

        done_a = await rec.wait_kind(sub_a, "turn_completed")
        assert done_a.msg.data["end_reason"] == "completed"
        hot = [(it.kind, it.payload.get("text")) for it in engine.history_snapshot()]
        assert hot == [("user_message", "A"), ("assistant_message", "A")]
    finally:
        await pool.close()


async def test_compact_now_does_not_starve_cancel_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """根 turn 在飞时 CompactNow 排队（不内联阻塞 actor），随后 CancelTurn(根) 立即生效。"""
    client = SimClient(turns=[
        SimTurn(text="slow", delay_seconds=2.0), SimTurn(text="after"),
    ])
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        rec = _Recorder(engine)
        await asyncio.sleep(0)
        sub_a = await engine.submit(taifeng.UserMessage(text="A"))
        await asyncio.sleep(0.05)
        sub_c = await engine.submit(CompactNow())
        await rec.wait_kind(sub_c, "submission_queued")
        token_a = engine._pending[sub_a].cancel  # noqa: SLF001
        await engine.submit(CancelTurn(submission_id=sub_a))
        # 饿死判据：actor 是否及时处理了 CancelTurn（token 置位），而非 turn 何时退出
        # （SimClient 的 delay 睡眠本身不响应 token，睡完才检查）
        for _ in range(30):
            if token_a.is_cancelled:
                break
            await asyncio.sleep(0.01)
        assert token_a.is_cancelled, "CancelTurn 被 CompactNow 饿死（actor 未及时处理）"
        done_a = await rec.wait_kind(sub_a, "turn_completed", timeout=4.0)
        assert done_a.msg.data["end_reason"] == "cancelled"
    finally:
        await pool.close()


async def test_rollback_waits_for_inflight_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """ThreadRollback 在飞时排队，A 完成后才回滚 → 最终 history 为空（回滚了 A）。"""
    client = SimClient(turns=[SimTurn(text="A", delay_seconds=0.3)])
    pool = await _pool(skills_dir, threads_dir, client)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        rec = _Recorder(engine)
        await asyncio.sleep(0)
        sub_a = await engine.submit(taifeng.UserMessage(text="A"))
        await asyncio.sleep(0.05)
        sub_r = await engine.submit(ThreadRollback(num_turns=1))
        await rec.wait_kind(sub_r, "submission_queued")
        await rec.wait_kind(sub_a, "turn_completed")
        await asyncio.sleep(0.2)
        assert engine.history_snapshot() == []
    finally:
        await pool.close()
