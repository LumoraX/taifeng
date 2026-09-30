"""审计模式下的上下文压缩与预算提示（ADR 0094）。

折叠式压缩的结论与摘要条目同批落账；为摘要发起的 LLM 调用按普通 LLM effect 落账；
ack 之后才改 hot history；恢复时按压缩标记重建出同样的逻辑 history。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.budget import POST_COMPACTION_TOKENS_KEY, ContextBudget
from taifeng.context.compressor import CompressionOrchestrator
from taifeng.context.strategies import (
    HandoffCompactionStrategy,
    SlidingWindowStrategy,
    SurgicalTrimStrategy,
)
from taifeng.context.strategies.multimodal_evict import MultimodalEvictionStrategy
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.context_records import (
    COMPACTION_LLM_ITERATION_BASE,
    BudgetHintInjectedV1,
    ContextCompactedV1,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.records import (
    JournalIdentities,
    deserialize_response_item,
    record_id,
    serialize_response_item,
)
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    compacted,
    system_injection,
    user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_compaction import superseded_item_ids
from taifeng.loop.audit_config import (
    AuditCapabilityError,
    AuditConfig,
    AuditStaticInputs,
    _validate_unsupported_fields,
)
from taifeng.loop.audit_history import AuditedHistoryConflictError, merge_audited_history
from tests.conftest import run_until_root_done

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope

_SESSION = "ses-compaction"
_TID = "thr-x"
_LIGHT = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
# provider 回报的输入量越过窗口 1200 的硬阈值（1140）：下一次采样前触发压缩
_HEAVY = TokenUsage(input_tokens=1150, output_tokens=5, total_tokens=1155)
# 越过软阈值（1020）但不到硬阈值：只触发预算提示 / 摘要式压缩
_WARM = TokenUsage(input_tokens=1050, output_tokens=5, total_tokens=1055)

_SKILL = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [read_skill]
max_call_depth: 2
---
# 入口
"""

_SUMMARY = """## 目标
整理季度报告。

## 已完成
- 收集了三个部门的数据。

## 关键决定
- 采用方案乙。

## 待办
- 汇总成表。
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True, exist_ok=True)
    (root / "entry" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return root


def _say(text: str, usage: TokenUsage = _LIGHT) -> SimTurn:
    return SimTurn(text=text, usage=usage)


class _Run:
    """一个审计 Session：pool / engine / 模拟客户端。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.sim: SimClient
        self.core: JsonlSessionJournalCore

    async def start(
        self,
        turns: list[SimTurn],
        compressors: list[Any],
        *,
        resume_thread_id: str | None = None,
        handoff: bool = False,
    ) -> None:
        self.sim = SimClient(turns=turns)
        client = AttemptObservableClientAdapter(
            self.sim, provider="sim", default_model="sim-model"
        )
        if handoff:
            compressors = [HandoffCompactionStrategy(model_client=client)]
        self.core = JsonlSessionJournalCore(self.tmp_path / "journal")
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(self.tmp_path),
            threads_dir=self.tmp_path / "threads",
            model_client=client,
            compressors=compressors,
            budget=ContextBudget(context_window=1200, preserve_tail_messages=2),
            audit=AuditConfig(
                journal_core=self.core,
                writer_id="writer-compaction",
                max_attachment_bytes=65536,
                max_total_attachment_bytes=1048576,
            ),
        )
        self.engine = await self.pool.get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )

    async def ask(self, text: str) -> list[Any]:
        events = await run_until_root_done(self.engine, taifeng.UserMessage(text=text))
        assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
        return events

    async def journal(self) -> list[JournalEnvelope]:
        core = JsonlSessionJournalCore(self.tmp_path / "journal")
        return [envelope async for envelope in core.load(_SESSION)]

    async def projection(self) -> list[ResponseItem]:
        raw = [i async for i in await self.pool.store.load_thread(self.engine.thread_id)]
        return reconstruct_logical_history(raw)

    def kinds(self) -> list[str]:
        return [item.kind for item in self.engine.history_snapshot()]

    async def crash(self) -> str:
        """写者消失、没有 session_ended：返回 root thread id。"""
        thread_id = self.engine.thread_id
        await self.core.close()
        with pytest.raises(AuditSessionReleaseError):
            await self.pool.close()
        return thread_id


def _data(events: list[Any], kind: str) -> list[dict[str, Any]]:
    return [dict(event.msg.data) for event in events if event.msg.kind == kind]


def _of(envelopes: list[JournalEnvelope], record_type: str) -> list[JournalEnvelope]:
    return [e for e in envelopes if e.record_type == record_type]


async def _talk_until_compacted(run: _Run, compressors: list[Any]) -> list[Any]:
    """跑四轮把 history 撑过硬阈值，第五轮采样前触发压缩；返回第五轮的事件。"""
    await run.start(
        [_say("回答一"), _say("回答二"), _say("回答三"), _say("回答四", _HEAVY), _say("回答五")],
        compressors,
    )
    for index in ("一", "二", "三", "四"):
        await run.ask(f"问题{index}")
    return await run.ask("问题五")


# ====================================================================
# 静态门
# ====================================================================


def _inputs(compressor: object) -> AuditStaticInputs:
    return AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False,
        skill_suspension_enabled=False,
        compressor=compressor,
    )


def test_fold_strategies_are_admitted() -> None:
    client = AttemptObservableClientAdapter(
        SimClient(turns=[]), provider="sim", default_model="m"
    )
    orchestrator = CompressionOrchestrator([
        SlidingWindowStrategy(), HandoffCompactionStrategy(model_client=client),
    ])

    _validate_unsupported_fields(_inputs(orchestrator))


@pytest.mark.parametrize(
    "strategy", [SurgicalTrimStrategy(), MultimodalEvictionStrategy()],
)
def test_rewriting_strategies_are_rejected(strategy: Any) -> None:
    """原地改写条目的策略：改写后的条目不进 Journal，审计模式不接受。"""
    orchestrator = CompressionOrchestrator([SlidingWindowStrategy(), strategy])

    with pytest.raises(AuditCapabilityError) as raised:
        _validate_unsupported_fields(_inputs(orchestrator))

    assert raised.value.code == "audit_compressor_unsupported"


def test_strategy_without_declaration_is_rejected() -> None:
    class _Undeclared(SlidingWindowStrategy):
        audit_support = "rewrite"  # type: ignore[assignment]

    with pytest.raises(AuditCapabilityError):
        _validate_unsupported_fields(_inputs(CompressionOrchestrator([_Undeclared()])))
    with pytest.raises(AuditCapabilityError):
        _validate_unsupported_fields(_inputs(object()))


# ====================================================================
# 折叠式压缩
# ====================================================================


async def test_sliding_compaction_is_journaled_with_its_summary_item(tmp_path: Path) -> None:
    run = _Run(tmp_path)

    events = await _talk_until_compacted(run, [SlidingWindowStrategy(keep_tail=2)])

    completed = _data(events, "compaction_completed")
    assert [c["success"] for c in completed] == [True]
    envelopes = await run.journal()
    (record,) = _of(envelopes, "context_compacted")
    payload = ContextCompactedV1.model_validate(record.payload)
    assert (payload.phase, payload.strategy, payload.ordinal) == ("pre_turn", "sliding", 0)
    assert payload.removed_item_count == payload.replaced_range[1] - payload.replaced_range[0]
    assert payload.tokens_before >= 1140
    assert payload.tokens_after < payload.tokens_before
    assert payload.llm_request_record_ids == ()
    # 摘要条目与压缩结论同批、紧随其后，并回指压缩结论
    (summary,) = [
        e for e in _of(envelopes, "conversation_item")
        if e.payload["item_kind"] == "compacted"
    ]
    assert summary.seq == record.seq + 1
    assert summary.operation_id == record.operation_id
    assert summary.payload["source_record_id"] == record.record_id
    assert summary.payload["item_id"] == payload.summary_item_id
    assert summary.payload["metadata"][POST_COMPACTION_TOKENS_KEY] == payload.tokens_after
    assert record.operation_id.endswith(":turn:4:compaction:0")
    await run.pool.close()


async def test_hot_history_projection_and_journal_agree(tmp_path: Path) -> None:
    run = _Run(tmp_path)

    await _talk_until_compacted(run, [SlidingWindowStrategy(keep_tail=2)])

    history = list(run.engine.history_snapshot())
    assert "compacted" in run.kinds()
    assert len(history) < 10
    # 被折叠的条目没有在回写时被并回来
    assert run.kinds().count("user_message") < 5
    assert await run.projection() == history
    journaled = [
        deserialize_response_item_from(e) for e in _of(await run.journal(), "conversation_item")
    ]
    assert reconstruct_logical_history(journaled) == history
    # 模型在第五轮看到的是折叠后的上下文
    sent = run.sim.ledger.requests()[-1].request
    assert any("已省略" in str(message.content) for message in sent.messages)
    await run.pool.close()


def deserialize_response_item_from(envelope: JournalEnvelope) -> ResponseItem:
    from taifeng.conversation.journal.records import ConversationItemV1

    return deserialize_response_item(ConversationItemV1.model_validate(envelope.payload))


async def test_journal_verifies_after_compaction(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _talk_until_compacted(run, [SlidingWindowStrategy(keep_tail=2)])
    await run.pool.close()

    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)

    assert verification.health is JournalHealth.HEALTHY
    assert not verification.physical_tail_torn


async def test_resume_rebuilds_the_folded_history(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _talk_until_compacted(run, [SlidingWindowStrategy(keep_tail=2)])
    before = list(run.engine.history_snapshot())
    thread_id = await run.crash()

    resumed = _Run(tmp_path)
    await resumed.start(
        [_say("回答六")], [SlidingWindowStrategy(keep_tail=2)], resume_thread_id=thread_id,
    )

    assert list(resumed.engine.history_snapshot()) == before
    await resumed.ask("问题六")
    sent = resumed.sim.ledger.requests()[-1].request
    assert any("已省略" in str(message.content) for message in sent.messages)
    assert not any("问题一" in str(message.content) for message in sent.messages)
    await resumed.pool.close()


async def test_second_compaction_in_a_later_turn_gets_its_own_identity(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        [
            _say("回答一"), _say("回答二", _HEAVY), _say("回答三"),
            _say("回答四", _HEAVY), _say("回答五"),
        ],
        [SlidingWindowStrategy(keep_tail=2)],
    )
    for index in ("一", "二", "三", "四", "五"):
        await run.ask(f"问题{index}")

    records = _of(await run.journal(), "context_compacted")

    assert len(records) == 2
    assert len({record.record_id for record in records}) == 2
    assert all(record.payload["ordinal"] == 0 for record in records)
    assert await run.projection() == list(run.engine.history_snapshot())
    await run.pool.close()


# ====================================================================
# 摘要式压缩：LLM 调用同样落账
# ====================================================================


async def test_handoff_summary_call_is_journaled_as_an_llm_effect(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        [
            _say("回答一"), _say("回答二"), _say("回答三", _WARM),
            SimTurn(text=_SUMMARY, usage=_LIGHT), _say("回答四"),
        ],
        [], handoff=True,
    )
    for index in ("一", "二", "三"):
        await run.ask(f"问题{index}")

    events = await run.ask("问题四")

    assert [c["success"] for c in _data(events, "compaction_completed")] == [True]
    envelopes = await run.journal()
    (record,) = _of(envelopes, "context_compacted")
    payload = ContextCompactedV1.model_validate(record.payload)
    assert payload.strategy == "handoff"
    (request_id,) = payload.llm_request_record_ids
    (request,) = [e for e in _of(envelopes, "llm_request_committed") if e.record_id == request_id]
    assert request.payload["iteration"] == COMPACTION_LLM_ITERATION_BASE
    assert request.operation_id.endswith(f":llm:{COMPACTION_LLM_ITERATION_BASE}")
    same_call = [e for e in envelopes if e.operation_id == request.operation_id]
    assert [e.record_type for e in same_call] == [
        "llm_request_committed", "llm_response_checkpoint", "llm_response_committed",
    ]
    # 顺序：摘要调用的三条记录 → 压缩结论 → 摘要条目
    assert same_call[-1].seq < record.seq
    summary = next(i for i in run.engine.history_snapshot() if i.kind == "compacted")
    assert "采用方案乙" in summary.payload["summary"]
    assert await run.projection() == list(run.engine.history_snapshot())
    await run.pool.close()


async def test_failed_summary_call_is_journaled_and_history_is_kept(tmp_path: Path) -> None:
    """摘要为空：压缩不应用，但那次 LLM 调用已经发生，照样落账。"""
    run = _Run(tmp_path)
    await run.start(
        [
            _say("回答一"), _say("回答二"), _say("回答三", _WARM),
            SimTurn(text="", usage=_LIGHT), _say("回答四"),
        ],
        [], handoff=True,
    )
    for index in ("一", "二", "三"):
        await run.ask(f"问题{index}")

    events = await run.ask("问题四")

    assert [c["success"] for c in _data(events, "compaction_completed")] == [False]
    envelopes = await run.journal()
    assert _of(envelopes, "context_compacted") == []
    calls = [
        e for e in _of(envelopes, "llm_response_committed")
        if e.operation_id.endswith(f":llm:{COMPACTION_LLM_ITERATION_BASE}")
    ]
    assert len(calls) == 1
    assert "compacted" not in run.kinds()
    assert run.kinds().count("user_message") == 4
    await run.pool.close()


# ====================================================================
# 预算提示
# ====================================================================


async def test_budget_hint_is_journaled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([_say("回答一", _WARM), _say("回答二")], [])
    await run.ask("问题一")

    events = await run.ask("问题二")

    assert len(_data(events, "budget_hint_injected")) == 1
    envelopes = await run.journal()
    (record,) = _of(envelopes, "budget_hint_injected")
    payload = BudgetHintInjectedV1.model_validate(record.payload)
    assert (payload.context_window, payload.soft_limit, payload.hard_limit) == (1200, 1020, 1140)
    assert payload.used_tokens >= 1020
    (note,) = [
        e for e in _of(envelopes, "conversation_item")
        if e.payload["item_kind"] == "system_injection"
    ]
    assert note.seq == record.seq + 1
    assert note.payload["item_id"] == payload.item_id
    assert note.payload["payload"]["source"] == "budget_hint"
    assert record.operation_id.endswith(":turn:1:budget_hint:0")
    assert "system_injection" in run.kinds()
    assert await run.projection() == list(run.engine.history_snapshot())
    await run.pool.close()


# ====================================================================
# 记录与标识
# ====================================================================


def test_context_operation_identity() -> None:
    identities = JournalIdentities("ses", "thr", "sub")
    turn = identities.turn(3)

    operation = identities.context(turn, "compaction", 2)

    assert operation == "thr:sub:turn:3:compaction:2"
    assert record_id(operation, "context_compacted") == f"{operation}:context_compacted:none:0"
    with pytest.raises(ValueError, match="unknown context operation"):
        identities.context(turn, "rewind", 0)
    with pytest.raises(ValueError, match="non-negative"):
        identities.context(turn, "compaction", -1)
    with pytest.raises(ValueError, match="canonical turn"):
        identities.context("other:sub:turn:3", "compaction", 0)
    with pytest.raises(ValueError, match="not canonical"):
        record_id("thr:sub:turn:3:compaction:02", "context_compacted")


def _compacted_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phase": "pre_turn", "strategy": "sliding", "ordinal": 0,
        "tokens_before": 900, "tokens_after": 300, "replaced_range": (1, 4),
        "removed_item_count": 3, "summary_item_id": "item_1",
        "cache_invalidated": True, "anchor_preserved_until": -1,
    }
    return {**base, **overrides}


def test_context_compacted_payload_is_validated() -> None:
    ContextCompactedV1(**_compacted_payload())
    for overrides in (
        {"replaced_range": (4, 4), "removed_item_count": 1},
        {"replaced_range": (-1, 2), "removed_item_count": 3},
        {"removed_item_count": 2},
        {"phase": "manual"},
        {"phase": "overflow"},
        {"strategy": ""},
        {"anchor_preserved_until": -2},
        {"unexpected": 1},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            ContextCompactedV1(**_compacted_payload(**overrides))


def test_compacted_and_budget_hint_items_round_trip() -> None:
    summary = compacted("摘要", thread_id=_TID, replaced_range=(0, 3), cache_invalidated=True)
    note = system_injection("已用 90%", thread_id=_TID, source="budget_hint")

    for item in (summary, note):
        wire = serialize_response_item(item, source_record_id="src")
        assert deserialize_response_item(wire) == item


@pytest.mark.parametrize(
    "item",
    [
        ResponseItem(kind="compacted", thread_id=_TID, payload={"summary": "x"}),
        ResponseItem(kind="compacted", thread_id=_TID, payload={
            "summary": "x", "replaced_range": [3, 1], "cache_invalidated": False,
        }),
        ResponseItem(kind="compacted", thread_id=_TID, payload={
            "summary": "x", "replaced_range": [0, 1, 2], "cache_invalidated": False,
        }),
        # 截断类 marker 会改写 history：审计模式不落账
        ResponseItem(kind="system_injection", thread_id=_TID, payload={
            "text": "x", "source": "rewind", "cut_index": 2,
        }),
        # 业务侧运行时注入不在审计能力面内
        ResponseItem(kind="system_injection", thread_id=_TID, payload={
            "text": "x", "source": "runtime_injection",
        }),
    ],
)
def test_malformed_context_items_are_rejected(item: ResponseItem) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        serialize_response_item(item, source_record_id="src")


# ====================================================================
# hot history 回写
# ====================================================================


def _conversation() -> list[ResponseItem]:
    return [
        user_message("问题一", thread_id=_TID),
        assistant_message("回答一", thread_id=_TID, model="m"),
        user_message("问题二", thread_id=_TID),
        assistant_message("回答二", thread_id=_TID, model="m"),
    ]


def test_folded_items_are_not_merged_back() -> None:
    before = _conversation()
    summary = compacted("摘要", thread_id=_TID, replaced_range=(0, 2), cache_invalidated=True)
    answer = assistant_message("回答三", thread_id=_TID, model="m")
    after = [summary, *before[2:], answer]
    superseded = superseded_item_ids(before, [summary, *before[2:]])

    merged = merge_audited_history(before, after, superseded=superseded)

    assert superseded == {before[0].id, before[1].id}
    assert merged == after


def test_input_applied_meanwhile_is_kept_after_a_compaction() -> None:
    before = _conversation()
    summary = compacted("摘要", thread_id=_TID, replaced_range=(0, 2), cache_invalidated=True)
    late = user_message("排队中的输入", thread_id=_TID)

    merged = merge_audited_history(
        [*before, late], [summary, *before[2:]],
        superseded=superseded_item_ids(before, [summary, *before[2:]]),
    )

    assert merged == [summary, *before[2:], late]


def test_conflict_is_still_detected_after_a_compaction() -> None:
    before = _conversation()
    summary = compacted("摘要", thread_id=_TID, replaced_range=(0, 2), cache_invalidated=True)
    tampered = before[2].model_copy(update={"payload": {"text": "被改过", "attachments": []}})

    with pytest.raises(AuditedHistoryConflictError):
        merge_audited_history(
            before, [summary, tampered, before[3]],
            superseded={before[0].id, before[1].id},
        )


def test_merge_without_compaction_is_unchanged() -> None:
    before = _conversation()
    answer = assistant_message("回答三", thread_id=_TID, model="m")

    assert merge_audited_history(before, [*before, answer]) == [*before, answer]
