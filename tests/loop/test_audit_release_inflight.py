"""释放审计 Session 时在飞的 root turn（ADR 0102）：先协作取消，意图收敛为终态后才终结。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_resume_scan import find_unsettled_effects
from tests.conftest import wait_for_condition
from tests.loop.test_audit_spawn import _SESSION, _call, _of, _Run

if TYPE_CHECKING:
    from pathlib import Path


async def test_a_tool_call_in_flight_at_release_gets_a_cancelled_outcome(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(root=[SimTurn(tool_calls=[_call("slow", "r1")])])
    await run.engine.submit(taifeng.UserMessage(text="一"))
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)

    await run.pool.close()

    envelopes = await run.journal()
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "cancelled"
    assert outcome.seq < _of(envelopes, "session_ended")[0].seq
    assert find_unsettled_effects(envelopes) == ()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_an_llm_call_in_flight_at_release_is_checkpointed_as_cancelled(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(root=[SimTurn(text="第一轮", await_signal="go")])
    await run.engine.submit(taifeng.UserMessage(text="一"))
    await wait_for_condition(lambda: len(run.sim.ledger.requests()) == 1)

    await run.pool.close()

    envelopes = await run.journal()
    (checkpoint,) = _of(envelopes, "llm_response_checkpoint")
    assert checkpoint.payload["status"] == "cancelled"
    assert find_unsettled_effects(envelopes) == ()
    assert len(_of(envelopes, "session_ended")) == 1
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
