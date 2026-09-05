"""晚到订阅者的终态补投契约。

背景：`subscribe(submission_id)` 是 live-only（每次调用新建空队列、不回放历史），
而 engine 原先只记 `_closed`、不记 per-submission 终态。于是「submission 已终态之后
才订阅」的消费者永远等不到终结信号，只能挂到调用方自己的 timeout——2026-09-04 CI
与本地全量跑的间歇 `TimeoutError` 即由此而来。

本文件锁住：已终态 → 立即补投**真实终态事件**；未知/在飞 submission → 维持等待
（`subscribe` 早于 `submit` 是推荐用法，绝不能被合成终结打断）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.event import EventMsg, TurnFailed, TurnSuspended

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = "code-reviewer"


async def _pool(
    skills_dir: Path, threads_dir: Path, client: SimClient, **kwargs: Any
) -> taifeng.EnginePool:
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], **kwargs,
    )


async def _drain(engine: Any, sub_id: str, deadline_seconds: float) -> list[Any]:
    """收集到终态为止；超时即判定「挂死」。"""
    async def _run() -> list[Any]:
        seen = []
        async for env in engine.subscribe_envelopes(sub_id):
            seen.append(env)
            if env.event.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
                break
        return seen
    return await asyncio.wait_for(_run(), timeout=deadline_seconds)


async def _run_one_turn(engine: Any, text: str = "A") -> str:
    """提交一个 turn 并等它彻底跑完（订阅建立在 submit 之前，正常用法）。"""
    sub = await engine.submit(taifeng.UserMessage(text=text))
    await _drain(engine, sub, deadline_seconds=5.0)
    return str(sub)


# --- 已终态 → 立即补投真实终态 ---------------------------------------------


async def test_late_subscriber_receives_terminal_instead_of_hanging(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """submission 早已 turn_completed，之后才订阅 —— 必须立刻拿到终结，不得挂死。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub = await _run_one_turn(engine)
        await asyncio.sleep(0.3)  # 确保终态早已发生

        seen = await _drain(engine, sub, deadline_seconds=1.5)

        assert [e.event.msg.kind for e in seen] == ["turn_completed"]
        assert seen[0].event.submission_id == sub
    finally:
        await pool.close()


async def test_replayed_terminal_is_the_real_event_not_a_synthetic_one(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """补投的必须是**当时那条**终态事件（含 data），不是笼统的「结束了」。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub = await engine.submit(taifeng.UserMessage(text="A"))
        live = await _drain(engine, sub, deadline_seconds=5.0)
        original = live[-1].event

        replayed = (await _drain(engine, sub, deadline_seconds=1.5))[0].event

        assert replayed.msg.kind == original.msg.kind
        assert replayed.msg.data == original.msg.data
        assert replayed.seq == original.seq  # 全局 seq 是事件身份，补投不重新分配
    finally:
        await pool.close()


async def test_replayed_delivery_seq_starts_at_zero(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """补投走正常投递簿记：新订阅者的 delivery_seq 仍从 0 起。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub = await _run_one_turn(engine)
        seen = await _drain(engine, sub, deadline_seconds=1.5)
        assert seen[0].delivery_seq == 0
    finally:
        await pool.close()


@pytest.mark.parametrize(
    "terminal",
    [
        TurnFailed(data={"error": "boom", "kind": "X", "is_root": True}),
        TurnSuspended(data={"record_id": "r1"}),
    ],
)
async def test_all_terminal_kinds_are_recorded(
    skills_dir: Path, threads_dir: Path, terminal: Any,
) -> None:
    """三种终结 kind 都要记账；此处直接驱动事件总线覆盖 failed / suspended。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        await engine._emit(EventMsg(submission_id="sub_x", msg=terminal))  # noqa: SLF001

        seen = await _drain(engine, "sub_x", deadline_seconds=1.5)
        assert [e.event.msg.kind for e in seen] == [terminal.kind]
    finally:
        await pool.close()


# --- 未知 / 在飞 submission 不得被合成终结打断 -------------------------------


async def test_unknown_submission_still_waits(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """subscribe 早于 submit 是推荐用法 —— 未知 id 必须继续等，不能立刻合成终结。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        with pytest.raises(TimeoutError):
            await _drain(engine, "never_submitted", deadline_seconds=0.4)
    finally:
        await pool.close()


async def test_subscribe_before_submit_still_sees_full_stream(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """对照：正常订阅顺序的事件流不受本次改动影响。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]))
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub = await engine.submit(taifeng.UserMessage(text="A"))
        seen = await _drain(engine, sub, deadline_seconds=5.0)
        kinds = [e.event.msg.kind for e in seen]
        assert kinds[0] == "turn_started"
        assert kinds[-1] == "turn_completed"
        assert len(kinds) > 1  # 不是只拿到一条补投
    finally:
        await pool.close()


# --- 有界：不得无限增长 -----------------------------------------------------


async def test_terminal_replay_cache_is_bounded(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """记账有上限；超出后最老的被淘汰（退化回等待，而不是无限吃内存）。"""
    client = SimClient(turns=[SimTurn(text="A")] * 6)
    pool = await _pool(skills_dir, threads_dir, client, terminal_replay_size=2)
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        subs = [await _run_one_turn(engine, f"m{i}") for i in range(3)]

        # 最近两个仍可补投
        for sub in subs[1:]:
            seen = await _drain(engine, sub, deadline_seconds=1.5)
            assert seen[0].event.msg.kind == "turn_completed"
        # 最老的已被淘汰 → 回到「等待」语义
        with pytest.raises(TimeoutError):
            await _drain(engine, subs[0], deadline_seconds=0.4)
    finally:
        await pool.close()


async def test_zero_cap_disables_replay(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """terminal_replay_size=0 = 关闭补投（保留历史行为的逃生口）。"""
    pool = await _pool(
        skills_dir, threads_dir, SimClient(turns=[SimTurn(text="A")]),
        terminal_replay_size=0,
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id=ENTRY)
        sub = await _run_one_turn(engine)
        with pytest.raises(TimeoutError):
            await _drain(engine, sub, deadline_seconds=0.4)
    finally:
        await pool.close()
