"""审计模式下的 peer 消息（ADR 0100）：发出落账，由目标 thread 的写者写进对话。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.peer_records import PeerMessageSentV1
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_peer import PEER_RECORD_KEY, undelivered_peer_messages
from taifeng.loop.audit_resume_scan import find_unsettled_effects
from taifeng.loop.audit_spawn import child_thread_id_of
from tests.loop.test_audit_spawn import _SESSION, _call, _of, _Run

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope


def _messages(envelopes: list[JournalEnvelope], thread_id: str) -> list[JournalEnvelope]:
    """某 thread 上已进入对话的 peer 消息。"""
    return [
        e for e in _of(envelopes, "conversation_item")
        if e.thread_id == thread_id and e.payload["payload"].get("source") == "peer"
    ]


def _requests(run: _Run, marker: str) -> list[str]:
    """带某个 skill 正文标记的各次 LLM 请求的全文。"""
    blobs = [request.blob() for request in run.sim.ledger.requests()]
    return [blob for blob in blobs if marker in blob]


def _root_requests(run: _Run) -> list[str]:
    """root 发出的各次 LLM 请求的全文。"""
    return _requests(run, "ROOT-BODY")


async def test_child_message_reaches_the_running_parent_turn(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[
            SimTurn(tool_calls=[
                _call("spawn_skill", "c1", skill_id="worker", args={}, reason="并行"),
            ]),
            SimTurn(tool_calls=[_call("slow", "r1")]),
            SimTurn(text="收到了孩子的话"),
        ],
        worker=[
            SimTurn(tool_calls=[
                _call("send_message", "m1", target="parent", text="进度过半"),
            ]),
            SimTurn(text="干完了"),
        ],
    )
    # root 也要能用 slow：借它把第二次迭代拖到消息到达之后
    turn = asyncio.create_task(run.ask())
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    root_thread = run.engine.thread_id

    async def sent() -> bool:
        return len(_of(await run.journal(), "peer_message_sent")) == 1

    for _ in range(500):
        if await sent():
            break
        await asyncio.sleep(0.01)
    # 发出已落账，还没有进入 root 的对话：root 的写者停在工具调用里
    assert _messages(await run.journal(), root_thread) == []
    run.slow_release.set()
    events = await asyncio.wait_for(turn, timeout=15)

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await run.journal()
    (record,) = _of(envelopes, "peer_message_sent")
    payload = PeerMessageSentV1.model_validate(record.payload)
    (handle_id,) = [str(e.payload["handle_id"]) for e in _of(envelopes, "spawn_started")]
    child_thread = child_thread_id_of(_SESSION, handle_id)
    assert (payload.from_thread_id, payload.to_thread_id) == (child_thread, root_thread)
    assert (payload.mode, payload.mode_downgraded) == ("queue_only", False)
    assert record.thread_id == child_thread
    assert payload.item.payload == {
        "text": "进度过半", "attachments": [], "source": "peer", "from_thread": child_thread,
    }
    # 发出先于发送方那次工具调用的结算
    (outcome,) = [
        e for e in _of(envelopes, "tool_outcome_committed") if e.payload["call_id"] == "m1"
    ]
    assert record.seq < outcome.seq
    assert json.loads(outcome.payload["output"])["delivered_via"] == "pending_input"
    # 进入对话：在 root 的工具调用结算之后、下一次采样之前，回指发出记录
    (delivered,) = _messages(envelopes, root_thread)
    assert delivered.payload["source_record_id"] == record.record_id
    assert delivered.payload["item_id"] == payload.message_id
    assert delivered.payload["metadata"][PEER_RECORD_KEY] == record.record_id
    (slow_outcome,) = [
        e for e in _of(envelopes, "tool_outcome_committed") if e.payload["call_id"] == "r1"
    ]
    last_request = [
        e for e in _of(envelopes, "llm_request_committed") if e.thread_id == root_thread
    ][-1]
    assert slow_outcome.seq < delivered.seq < last_request.seq
    assert "进度过半" in _root_requests(run)[-1]
    # hot history、投影与 Journal 的顺序一致
    projected = [i async for i in await run.pool.store.load_thread(root_thread)]
    assert projected == list(run.engine.history_snapshot())
    assert undelivered_peer_messages(envelopes, root_thread) == ()
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_parent_message_reaches_a_running_child(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[
            SimTurn(tool_calls=[
                _call("spawn_skill", "c1", skill_id="worker", args={}, reason="并行"),
            ]),
            SimTurn(tool_calls=[_call(
                "send_message", "m1", target="child:worker", text="补充说明",
                mode="trigger_turn",
            )]),
            SimTurn(text="已转告"),
        ],
        worker=[
            SimTurn(tool_calls=[_call("slow", "s1")]),
            SimTurn(text="按补充说明办了"),
        ],
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    run.slow_release.set()
    envelopes = await run.journal()
    (handle_id,) = [str(e.payload["handle_id"]) for e in _of(envelopes, "spawn_started")]
    await run.settled(handle_id, "done")
    envelopes = await run.journal()
    child_thread = child_thread_id_of(_SESSION, handle_id)
    (record,) = _of(envelopes, "peer_message_sent")
    payload = PeerMessageSentV1.model_validate(record.payload)
    assert (payload.from_thread_id, payload.to_thread_id) == (run.engine.thread_id, child_thread)
    # 目标正在运行：要求唤醒、实际排队
    assert (payload.mode, payload.mode_downgraded) == ("trigger_turn", True)
    assert payload.address == "child:worker"
    (delivered,) = _messages(envelopes, child_thread)
    (slow_outcome,) = [
        e for e in _of(envelopes, "tool_outcome_committed") if e.payload["call_id"] == "s1"
    ]
    (settled,) = _of(envelopes, "spawn_settled")
    assert slow_outcome.seq < delivered.seq < settled.seq
    assert "补充说明" in _requests(run, "WORKER-BODY")[-1]
    child_items = [i async for i in await run.pool.store.load_thread(child_thread)]
    # 消息落在调用与结果配对之后，没有把一对调用拆开
    assert [i.kind for i in child_items] == [
        "user_message", "assistant_message", "function_call", "function_call_output",
        "user_message", "assistant_message",
    ]
    assert find_unsettled_effects(envelopes) == ()
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_message_to_an_idle_root_waits_for_the_next_turn(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="知道了")],
        worker=[SimTurn(tool_calls=[_call("slow", "s1")]), SimTurn(text="干完了")],
    )
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="后台")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    child_thread = out["child_thread_id"]
    root_thread = run.engine.thread_id

    result = await run.engine.deliver_peer_message(
        target="parent", text="有空看一下", from_thread_id=child_thread,
    )

    assert result["delivered_via"] == "inbox"
    envelopes = await run.journal()
    assert len(_of(envelopes, "peer_message_sent")) == 1
    assert _messages(envelopes, root_thread) == []
    (waiting,) = undelivered_peer_messages(envelopes, root_thread)
    assert waiting.payload["text"] == "有空看一下"
    events = await run.ask("在吗")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await run.journal()
    (delivered,) = _messages(envelopes, root_thread)
    kinds = [i.kind for i in run.engine.history_snapshot()]
    assert kinds == ["user_message", "user_message", "assistant_message"]
    assert "有空看一下" in _root_requests(run)[-1]
    assert delivered.payload["source_record_id"] == _of(
        envelopes, "peer_message_sent")[0].record_id
    run.slow_release.set()
    await run.settled(out["handle_id"], "done")
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_a_finished_child_accepts_nothing(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[SimTurn(text="干完了")])
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="先跑完")
    await run.settled(out["handle_id"], "done")

    for mode in ("queue_only", "trigger_turn"):
        with pytest.raises(ValueError, match="peer_target_not_running"):
            await run.engine.deliver_peer_message(
                target=out["handle_id"], text="还在吗", mode=mode,
            )

    envelopes = await run.journal()
    assert _of(envelopes, "peer_message_sent") == []
    assert len(_of(envelopes, "spawn_settled")) == 1
    await run.pool.close()


async def test_undelivered_messages_return_to_the_inbox_on_takeover(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")],
        worker=[SimTurn(tool_calls=[_call("slow", "s1")])],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="后台")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    await run.engine.deliver_peer_message(
        target="parent", text="崩溃前发的", from_thread_id=out["child_thread_id"],
    )
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="看到了")], resume_thread_id=thread_id)
    events = await resumed.ask("继续")

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await resumed.journal()
    (delivered,) = _messages(envelopes, thread_id)
    assert delivered.payload["payload"]["text"] == "崩溃前发的"
    assert len(_of(envelopes, "peer_message_sent")) == 1
    assert "崩溃前发的" in _root_requests(resumed)[-1]
    assert undelivered_peer_messages(envelopes, thread_id) == ()
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
