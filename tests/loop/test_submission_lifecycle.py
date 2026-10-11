"""提交入口到执行入口之间的取消与收尾回归。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import CancelTurn, InjectSystemMessage, InjectUserInput, Resume
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition
from tests.loop.test_root_turn_gate import ENTRY, _pool, _Recorder

if TYPE_CHECKING:
    from pathlib import Path


async def test_submission_is_visible_before_actor_dispatch(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """submit 返回就是取消可达点，无须等待 actor 开始执行。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="unused")]))
    try:
        engine = await pool.get_or_create(session_id="admission", entry_skill_id=ENTRY)
        submission_id = await engine.submit(taifeng.UserMessage(text="hello"))
        assert submission_id in engine._pending
    finally:
        await pool.close()


async def test_rejected_resume_subscription_ends_without_fake_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """Resume 被拒也是提交终态，不需要虚构 turn 才能结束订阅。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[]))
    try:
        engine = await pool.get_or_create(session_id="reject", entry_skill_id=ENTRY)
        submission_id = await engine.submit(Resume(
            thread_id=engine.thread_id, record_id="missing", resolutions={},
        ))
        async with asyncio.timeout(1):
            events = [event async for event in engine.subscribe(submission_id)]
        assert [event.msg.kind for event in events] == ["suspension_resolve_rejected"]
    finally:
        await pool.close()


async def test_back_to_back_cancel_never_starts_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """紧接 submit 的取消不会因执行任务尚未登记而丢失。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="unused")]))
    try:
        engine = await pool.get_or_create(session_id="cancel", entry_skill_id=ENTRY)
        submission_id = await engine.submit(taifeng.UserMessage(text="hello"))
        await engine.submit(CancelTurn(submission_id=submission_id))
        events = [event async for event in engine.subscribe(submission_id)]
        assert events[-1].msg.kind == "turn_failed"
        assert events[-1].msg.data["kind"] == "cancelled"
        assert "turn_started" not in [event.msg.kind for event in events]
    finally:
        await pool.close()


async def test_resolutions_skip_settled_requests(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """只在当前 record 内跳过已核销请求，未知请求仍整体拒绝。"""
    from taifeng.conversation.models import function_call_output
    from taifeng.suspend.reason import PendingRequest, SuspendReason
    from taifeng.suspend.record import SuspensionRecord
    from taifeng.suspend.resolver import ResolveError

    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[]))
    try:
        engine = await pool.get_or_create(session_id="resolved", entry_skill_id=ENTRY)
        record = SuspensionRecord(
            record_id="record", thread_id=engine.thread_id, submission_id="original",
            turn_index=0, created_at=0, pending=tuple(
                PendingRequest(request_id=rid, reason=SuspendReason.DATA, related_call_id=rid)
                for rid in ("a", "b")
            ),
        )
        items = [record.to_item(), function_call_output("a", "done", thread_id=engine.thread_id)]
        assert engine._effective_resolutions(record, items, {"a": "changed", "b": "new"}) == {
            "b": "new",
        }
        with pytest.raises(ResolveError, match="already_resolved"):
            engine._effective_resolutions(record, items, {"a": "repeat"})
        with pytest.raises(ResolveError, match="unknown_request_ids"):
            engine._effective_resolutions(record, items, {"a": "repeat", "other": "unknown"})
        with pytest.raises(ResolveError, match="empty_resolutions"):
            engine._effective_resolutions(record, items, {})
    finally:
        await pool.close()


async def test_cancel_after_suspension_event_survives_writeback(
    skills_dir: Path, threads_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起事件发出后、runner 回写前取消，不能被迟到的 history 回写覆盖。"""
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
    from tests.loop.test_turn_terminal_usage import _write_entry_skill

    _write_entry_skill(skills_dir, "request_user_input")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[],
        model_client=SimClient(turns=[SimTurn(tool_calls=[{
            "id": "ask", "name": "request_user_input", "arguments": '{"prompt":"answer?"}',
        }])]), extra_tools=[make_request_user_input_tool()],
    )
    release = asyncio.Event()
    entered = asyncio.Event()
    try:
        engine = await pool.get_or_create(session_id="handoff", entry_skill_id="usage-skill")
        original = engine._drain_residual_injections

        async def paused_drain(runner, submission_id):
            """用同步屏障固定挂起事件与 engine 回写的窗口。"""
            entered.set()
            await release.wait()
            await original(runner, submission_id)

        monkeypatch.setattr(engine, "_drain_residual_injections", paused_drain)
        recorder = _Recorder(engine)
        await asyncio.sleep(0)
        target = await engine.submit(taifeng.UserMessage(text="ask"))
        await asyncio.wait_for(entered.wait(), GUARD_TIMEOUT_SECONDS)
        suspended = await recorder.wait_kind(target, "turn_suspended")
        cancel_id = await engine.submit(CancelTurn(submission_id=target))
        await recorder.wait_kind(cancel_id, "engine_log")
        release.set()
        await wait_for_condition(lambda: target not in engine._pending)
        assert engine._find_active_suspension() is None
        cancelled = await recorder.wait_kind(cancel_id, "suspension_cancelled")
        assert cancelled.msg.data["record_id"] == suspended.msg.data["record_id"]
        assert cancelled.msg.data["target_submission_id"] == target
        cold = [item async for item in await pool.store.load_thread(engine.thread_id)]
        assert [item.id for item in engine.history_snapshot()] == [item.id for item in cold]
    finally:
        release.set()
        await pool.close()


async def test_queued_input_is_persisted_without_claiming_delivery(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """尚未派发的提交只登记取消，输入沿用无活跃 turn 的持久化路径。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="done")]))
    try:
        engine = await pool.get_or_create(session_id="queued-input", entry_skill_id=ENTRY)
        recorder = _Recorder(engine)
        await asyncio.sleep(0)
        target = await engine.submit(taifeng.UserMessage(text="original"))
        injected = await engine.submit(InjectUserInput(submission_id=target, text="extra"))
        delivered = await recorder.wait_kind(injected, "user_input_injected")
        assert delivered.msg.data["delivered"] is False
        await recorder.wait_kind(target, "turn_completed")
        await wait_for_condition(lambda: target not in engine._pending)
        assert "extra" in [item.payload.get("text") for item in engine.history_snapshot()]
    finally:
        await pool.close()


@pytest.mark.parametrize("system_input", [False, True])
async def test_input_during_writeback_is_preserved(
    skills_dir: Path, threads_dir: Path, monkeypatch: pytest.MonkeyPatch, system_input: bool,
) -> None:
    """取消所有权延长到回写时，新增输入也必须收敛到热/冷历史。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[SimTurn(text="done")]))
    release = asyncio.Event()
    entered = asyncio.Event()
    try:
        engine = await pool.get_or_create(session_id="closing-input", entry_skill_id=ENTRY)
        original = engine._writeback_turn_runner

        async def paused_writeback(runner):
            """在回写前固定已完成首轮残余输入 drain 的窗口。"""
            entered.set()
            await release.wait()
            await original(runner)

        monkeypatch.setattr(engine, "_writeback_turn_runner", paused_writeback)
        recorder = _Recorder(engine)
        await asyncio.sleep(0)
        target = await engine.submit(taifeng.UserMessage(text="original"))
        await asyncio.wait_for(entered.wait(), GUARD_TIMEOUT_SECONDS)
        if system_input:
            await engine.submit(InjectSystemMessage(text="late input"))
            await wait_for_condition(lambda: bool(engine._pending[target].pending_input))
        else:
            injected = await engine.submit(InjectUserInput(submission_id=target, text="late input"))
            await recorder.wait_kind(injected, "user_input_injected")
        release.set()
        await wait_for_condition(lambda: target not in engine._pending)
        cold = [item async for item in await pool.store.load_thread(engine.thread_id)]
        assert "late input" in [item.payload.get("text") for item in cold]
        assert [item.id for item in engine.history_snapshot()] == [item.id for item in cold]
    finally:
        release.set()
        await pool.close()
