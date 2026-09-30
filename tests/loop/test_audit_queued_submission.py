"""审计模式下排队的用户消息（ADR 0101）：对话项在应用时落账，Journal 顺序就是对话顺序。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_resume_scan import find_unsettled_effects, rebuild_root_history
from tests.conftest import wait_for_condition
from tests.loop.test_audit_spawn import _SESSION, _call, _of, _Run

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope


def _texts(items: Any) -> list[tuple[str, str]]:
    return [(item.kind, str(item.payload.get("text"))) for item in items]


def _conversation(envelopes: list[JournalEnvelope], thread_id: str) -> list[tuple[str, str]]:
    return [
        (str(e.payload["item_kind"]), str(e.payload["payload"].get("text")))
        for e in _of(envelopes, "conversation_item") if e.thread_id == thread_id
    ]


class _Turns:
    """收集根 turn 的终态事件。"""

    def __init__(self, engine: Any) -> None:
        self.ended: list[tuple[str, str]] = []
        self._task = asyncio.create_task(self._collect(engine))

    async def _collect(self, engine: Any) -> None:
        async for ev in engine.subscribe_all():
            if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root"):
                self.ended.append((ev.submission_id, ev.msg.kind))

    async def wait(self, count: int) -> None:
        await wait_for_condition(lambda: len(self.ended) >= count)

    def stop(self) -> None:
        self._task.cancel()


async def test_a_queued_message_enters_the_conversation_when_its_turn_starts(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(root=[
        SimTurn(text="第一轮", await_signal="go"),
        SimTurn(text="第二轮"),
    ])
    turns = _Turns(run.engine)
    await asyncio.sleep(0)
    first = await run.engine.submit(taifeng.UserMessage(text="一"))
    await wait_for_condition(lambda: len(run.sim.ledger.requests()) == 1)

    second = await run.engine.submit(taifeng.UserMessage(text="二"))

    # 已准入、还在排队：准入记录已落账，对话项没有
    envelopes = await run.journal()
    assert [e.submission_id for e in _of(envelopes, "submission_accepted")] == [first, second]
    assert _conversation(envelopes, run.engine.thread_id) == [("user_message", "一")]
    assert [e.payload["accepted_record_id"] for e in _of(envelopes, "submission_applied")] == [
        _of(envelopes, "submission_accepted")[0].record_id,
    ]
    run.sim.coordinator.signal("go")
    await turns.wait(2)
    turns.stop()

    expected = [
        ("user_message", "一"), ("assistant_message", "第一轮"),
        ("user_message", "二"), ("assistant_message", "第二轮"),
    ]
    envelopes = await run.journal()
    thread_id = run.engine.thread_id
    # Journal 顺序、hot history、投影、接管时的重建，四者一致
    assert _conversation(envelopes, thread_id) == expected
    assert _texts(run.engine.history_snapshot()) == expected
    projected = [i async for i in await run.pool.store.load_thread(thread_id)]
    assert _texts(projected) == expected
    assert _texts(rebuild_root_history(envelopes, thread_id).items) == expected
    projection = run.engine._audit_state.projector.state(thread_id)  # noqa: SLF001
    assert projection.stale is False
    # 第一轮的模型没有看到排队的消息
    assert "二" not in run.sim.ledger.requests()[0].blob()
    # 应用批次：对话项与 applied 相邻，回指准入记录
    accepted = _of(envelopes, "submission_accepted")[1]
    item = next(e for e in _of(envelopes, "conversation_item") if e.submission_id == second)
    applied = envelopes[envelopes.index(item) + 1]
    assert applied.record_type == "submission_applied"
    assert item.payload["source_record_id"] == accepted.record_id
    assert applied.payload["accepted_record_id"] == accepted.record_id
    assert accepted.seq < item.seq
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_a_message_still_queued_at_release_is_applied_without_running(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(root=[SimTurn(tool_calls=[_call("slow", "r1")])])
    await run.engine.submit(taifeng.UserMessage(text="一"))
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    second = await run.engine.submit(taifeng.UserMessage(text="二"))
    thread_id = run.engine.thread_id

    await run.pool.close()

    envelopes = await run.journal()
    # 准入是 durable 承诺：没轮到的消息照样进入对话，位置在被取消的那个 turn 之后
    conversation = _conversation(envelopes, thread_id)
    assert conversation[0] == ("user_message", "一")
    assert conversation[-1] == ("user_message", "二")
    assert [kind for kind, _ in conversation].count("user_message") == 2
    applied = [e for e in _of(envelopes, "submission_applied") if e.submission_id == second]
    assert len(applied) == 1
    assert _texts(rebuild_root_history(envelopes, thread_id).items) == conversation
    # 它的 turn 没有运行
    assert all(
        e.submission_id != second for e in _of(envelopes, "llm_request_committed")
    )
    assert find_unsettled_effects(envelopes) == ()
    assert len(_of(envelopes, "session_ended")) == 1
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_messages_accepted_before_a_crash_are_applied_on_takeover(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(root=[SimTurn(tool_calls=[_call("slow", "r1")])])
    await run.engine.submit(taifeng.UserMessage(text="一"))
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    thread_id = run.engine.thread_id
    # 第一个 turn 停在工具调用里，后两条排在它后面：准入已 durable，应用没有发生
    late = [
        await run.engine.submit(taifeng.UserMessage(text=text)) for text in ("二", "三")
    ]
    crashed = await run.journal()
    assert [e.submission_id for e in _of(crashed, "submission_accepted")][1:] == late
    assert len(_of(crashed, "submission_applied")) == 1
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="都看到了")], resume_thread_id=thread_id)

    history = _texts(resumed.engine.history_snapshot())
    assert history[0] == ("user_message", "一")
    # 被中断的工具调用先收敛，排队的消息按准入顺序落在对话末尾
    assert history[-2:] == [("user_message", "二"), ("user_message", "三")]
    assert [kind for kind, _ in history].count("function_call_output") == 1
    envelopes = await resumed.journal()
    assert _conversation(envelopes, thread_id) == history
    assert find_unsettled_effects(envelopes) == ()
    recovered = [e for e in _of(envelopes, "submission_applied") if e.submission_id in late]
    assert [e.submission_id for e in recovered] == late
    assert all(e.correlation_id is not None for e in recovered)
    events = await resumed.ask("继续")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    blob = resumed.sim.ledger.requests()[-1].blob()
    assert "二" in blob and "三" in blob
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
