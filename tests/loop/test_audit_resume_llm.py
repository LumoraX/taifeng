"""接管时作废没有 checkpoint 的 LLM 请求（ADR 0103）：进程死在 LLM 调用途中的 Session 可以接管。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.recovery_records import LlmRequestAbandonedV1
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_resume_scan import find_unsettled_effects
from taifeng.loop.audit_spawn import child_thread_id_of
from tests.conftest import wait_for_condition
from tests.loop.test_audit_spawn import _SESSION, _call, _of, _Run

if TYPE_CHECKING:
    from pathlib import Path


async def _crash_mid_llm(run: _Run, expected_requests: int) -> str:
    """等 Sim 收到指定数量的请求（最后一个停在 await_signal 上），然后让进程「死掉」。"""
    await wait_for_condition(lambda: len(run.sim.ledger.requests()) == expected_requests)
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()
    return thread_id


async def test_a_root_turn_interrupted_mid_llm_is_abandoned_on_takeover(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(root=[SimTurn(text="永远不会说出口", await_signal="never")])
    await run.engine.submit(taifeng.UserMessage(text="一"))
    thread_id = await _crash_mid_llm(run, 1)
    crashed = await run.journal()
    (request,) = _of(crashed, "llm_request_committed")
    assert find_unsettled_effects(crashed) == (request.record_id,)

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着说")], resume_thread_id=thread_id)

    envelopes = await resumed.journal()
    (abandoned,) = _of(envelopes, "llm_request_abandoned")
    payload = LlmRequestAbandonedV1.model_validate(abandoned.payload)
    assert payload.request_record_id == request.record_id
    assert payload.reason == "process_recovery"
    assert abandoned.operation_id == request.operation_id
    assert abandoned.causation_id == request.record_id
    assert find_unsettled_effects(envelopes) == ()
    # 回复一个字也没进过对话
    assert [i.kind for i in resumed.engine.history_snapshot()] == ["user_message"]
    events = await resumed.ask("二")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert [i.kind for i in resumed.engine.history_snapshot()] == [
        "user_message", "user_message", "assistant_message",
    ]
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_a_spawn_interrupted_mid_llm_is_settled_on_takeover(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")],
        worker=[SimTurn(text="永远不会说出口", await_signal="never")],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="崩溃时在等模型")
    thread_id = await _crash_mid_llm(run, 2)
    child = child_thread_id_of(_SESSION, out["handle_id"])

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着来")], resume_thread_id=thread_id)

    envelopes = await resumed.journal()
    (abandoned,) = _of(envelopes, "llm_request_abandoned")
    assert abandoned.thread_id == child
    (settled,) = _of(envelopes, "spawn_settled")
    assert (settled.payload["status"], settled.payload["end_reason"]) == (
        "cancelled", "process_recovery")
    assert abandoned.seq < settled.seq
    assert resumed.status(out["handle_id"])["status"] == "cancelled"
    assert find_unsettled_effects(envelopes) == ()
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_a_synchronous_child_interrupted_mid_llm_is_settled_on_takeover(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(tool_calls=[
            _call("call_skill", "k1", skill_id="worker", args={}, reason="同步"),
        ])],
        worker=[SimTurn(text="永远不会说出口", await_signal="never")],
    )
    await run.engine.submit(taifeng.UserMessage(text="一"))
    thread_id = await _crash_mid_llm(run, 2)

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着来")], resume_thread_id=thread_id)

    envelopes = await resumed.journal()
    (abandoned,) = _of(envelopes, "llm_request_abandoned")
    assert abandoned.thread_id != thread_id
    (finished,) = _of(envelopes, "skill_dispatch_finished")
    assert finished.payload["status"] == "cancelled"
    (recovery,) = _of(envelopes, "tool_recovery_committed")
    assert recovery.payload["call_id"] == "k1"
    assert abandoned.seq < finished.seq < recovery.seq
    assert find_unsettled_effects(envelopes) == ()
    events = await resumed.ask("二")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
