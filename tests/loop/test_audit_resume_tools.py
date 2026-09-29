"""strict audit resume 收敛「结果未知」的工具调用 —— 引擎级端到端（ADR 0070）。

「崩溃」的模拟：工具 handler 执行途中 ``core.close()``——写者锁随之释放、intent 已 durable，
而 outcome 再也写不进去，这正是进程在工具执行期间死亡时 Journal 的真实形态。随后旧 pool 收尾
无法落 terminal。新 pool 用**另一个 core 实例**（模拟另一进程）指向同一 Journal 与 threads 目录
resume。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
import taifeng.loop.pool as pool_module
from taifeng.conversation.journal import JournalHealth, ToolRecoveryCommittedV1
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.audit_resume import AuditResumeError
from taifeng.loop.audit_resume_resolution import (
    AuditToolOutcomeRequest,
    AuditToolOutcomeResolution,
)
from taifeng.tool.spec import ReconcileVerdict, ToolResult, ToolSpec
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope

_SESSION = "ses-tool-recovery"
_SKILL = """---
name: ops-agent
description: 调用外部写接口的入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [remote_write]
max_call_depth: 2
---
# 入口
按需调用 remote_write。
"""
_CALL = {"id": "call-1", "name": "remote_write", "arguments": '{"key": "k1"}'}


def _skills(tmp_path: Path) -> Path:
    """写入只开放 remote_write 的入口 skill（重复调用幂等）。"""
    root = tmp_path / "skills"
    (root / "ops-agent").mkdir(parents=True, exist_ok=True)
    (root / "ops-agent" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return root


async def _never(args: dict[str, Any], ctx: object) -> ToolResult:
    """恢复路径绝不执行工具：被调用即测试失败。"""
    raise AssertionError("recovery must not execute the tool")


def _tool(
    *,
    handler: Any = _never,
    effect_kind: str = "external_non_idempotent",
    reconciliation: str = "manual",
    reconcile: Any = None,
    timeout_seconds: float = 5.0,
) -> ToolSpec:
    """构造满足 strict audit metadata 的 remote_write。"""
    return ToolSpec(
        name="remote_write",
        description="写外部系统",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler,
        effect_kind=effect_kind,
        reconciliation=reconciliation,
        reconcile=reconcile,
        timeout_seconds=timeout_seconds,
    )


async def _pool(
    tmp_path: Path,
    core: JsonlSessionJournalCore,
    turns: list[SimTurn],
    tool: ToolSpec,
    resolver: Any = None,
) -> tuple[taifeng.EnginePool, SimClient]:
    """同一 threads 目录 + 给定 core 实例的 audited pool。"""
    sim = SimClient(turns=turns)
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path),
        threads_dir=tmp_path / "threads",
        model_client=AttemptObservableClientAdapter(
            sim, provider="sim", default_model="sim-model"
        ),
        compressors=[],
        extra_tools=[tool],
        audit=AuditConfig(
            journal_core=core,
            writer_id="writer-recovery",
            max_attachment_bytes=65536,
            max_total_attachment_bytes=1048576,
            tool_outcome_resolver=resolver,
        ),
    )
    return pool, sim


async def _load(tmp_path: Path) -> list[JournalEnvelope]:
    """用独立 core 实例读出全部 committed envelopes。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    return [envelope async for envelope in core.load(_SESSION)]


async def _crash_mid_tool(tmp_path: Path) -> tuple[str, str]:
    """跑到 remote_write 执行途中「进程死亡」；返回 root thread id 与悬空 intent record id。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def crashing(args: dict[str, Any], ctx: object) -> ToolResult:
        # 写者消失：intent 已 durable，outcome 永远写不进去
        await core.close()
        return ToolResult.ok("written")

    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=[_CALL])], _tool(handler=crashing)
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1"))
    assert kind == "turn_failed"
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    envelopes = await _load(tmp_path)
    intents = [e for e in envelopes if e.record_type == "tool_intent_committed"]
    assert len(intents) == 1
    assert not [e for e in envelopes if e.record_type == "tool_outcome_committed"]
    return engine.thread_id, intents[0].record_id


async def _resume(
    tmp_path: Path, tool: ToolSpec, *, resolver: Any = None, turns: list[SimTurn] | None = None,
) -> tuple[taifeng.EnginePool, SimClient, taifeng.AgentEngine]:
    """以新 core 实例（模拟另一进程）resume 同一 root thread。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, sim = await _pool(tmp_path, core, list(turns or []), tool, resolver)
    thread_id = (await _load(tmp_path))[1].thread_id
    assert thread_id is not None
    engine = await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
    )
    return pool, sim, engine


def _recovery(envelopes: list[JournalEnvelope]) -> list[JournalEnvelope]:
    """筛出恢复结论记录。"""
    return [e for e in envelopes if e.record_type == "tool_recovery_committed"]


def _last_output(engine: taifeng.AgentEngine) -> taifeng.ResponseItem:
    """history 末项必须是补写的 function_call_output。"""
    item = engine.history_snapshot()[-1]
    assert item.kind == "function_call_output"
    assert item.payload["call_id"] == "call-1"
    return item


@pytest.mark.asyncio
async def test_resume_reconcile_completed_records_verdict_and_continues(
    tmp_path: Path,
) -> None:
    """回查确认已完成：durable 落结论 + 真实结果 output，续跑新 turn 时模型看到真实结果。"""
    thread_id, intent_id = await _crash_mid_tool(tmp_path)
    seen: list[tuple[dict[str, Any], str]] = []

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        seen.append((arguments, call_id))
        return ReconcileVerdict(status="completed", output="k1 已写入 rev=7")

    pool, sim, engine = await _resume(
        tmp_path,
        _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile),
        turns=[SimTurn(text="已确认写入")],
    )

    assert seen == [({"key": "k1"}, "call-1")]
    output = _last_output(engine)
    assert output.payload == {"call_id": "call-1", "output": "k1 已写入 rev=7", "is_error": False}
    assert output.metadata["recovered"] is True
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="继续")) == (
        "turn_completed"
    )
    assert sim.ledger.function_call_output_text("call-1") == "k1 已写入 rev=7"
    await pool.close()

    envelopes = await _load(tmp_path)
    types = [(e.writer_epoch, e.record_type) for e in envelopes]
    takeover = types.index((2, "writer_takeover"))
    assert types[takeover + 1: takeover + 3] == [
        (2, "tool_recovery_committed"),
        (2, "conversation_item"),
    ]
    (recovery,) = _recovery(envelopes)
    payload = ToolRecoveryCommittedV1.model_validate(recovery.payload)
    assert (payload.basis, payload.verdict, payload.intent_record_id) == (
        "reconcile", "completed", intent_id,
    )
    assert recovery.actor.kind == "system"
    assert recovery.causation_id == intent_id
    assert payload.recovery_operation_id == envelopes[takeover].operation_id
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    assert engine.thread_id == thread_id


@pytest.mark.asyncio
async def test_resume_reconcile_not_executed_tells_model_it_may_retry(tmp_path: Path) -> None:
    """回查确认未执行：补写「未执行，可安全重发」，处置为 safe_to_retry。"""
    await _crash_mid_tool(tmp_path)

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        return ReconcileVerdict(status="not_executed")

    pool, _, engine = await _resume(
        tmp_path, _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile)
    )

    output = _last_output(engine)
    assert output.payload["is_error"] is True
    assert "not executed" in output.payload["output"]
    await pool.close()
    (recovery,) = _recovery(await _load(tmp_path))
    assert recovery.payload["verdict"] == "not_executed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "raises"])
async def test_resume_reconcile_unresolved_without_resolver_fails_closed(
    tmp_path: Path, failure: str,
) -> None:
    """回查查不清 / 抛错且无人可问：拒绝并列出 intent，不写任何恢复记录，锁已释放。"""
    _, intent_id = await _crash_mid_tool(tmp_path)

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        if failure == "raises":
            raise ConnectionError("backend down")
        return ReconcileVerdict(status="unknown")

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(
        tmp_path, core, [],
        _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile),
    )
    thread_id = (await _load(tmp_path))[1].thread_id
    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
        )

    assert caught.value.code == "audit_resume_recovery_required"
    assert caught.value.record_ids == (intent_id,)
    assert _recovery(await _load(tmp_path)) == []
    assert pool._engines == {}  # noqa: SLF001
    probe = JsonlSessionJournalCore(tmp_path / "journal")
    await probe.open_existing(_SESSION, writer_id="probe", operation_id="probe")
    await probe.close()
    await pool.close()


@pytest.mark.asyncio
async def test_resume_idempotent_tool_recovered_as_safe_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落账与当前声明都为 idempotent：不回查、不执行，补写「可安全重发」并透出处置。"""
    captured: list[Any] = []
    original = pool_module.finalize_resumed_engine

    async def spy(engine: Any, **kwargs: Any) -> None:
        captured.append(kwargs["recovered_tool_calls"])
        await original(engine, **kwargs)

    monkeypatch.setattr(pool_module, "finalize_resumed_engine", spy)
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def crashing(args: dict[str, Any], ctx: object) -> ToolResult:
        await core.close()
        return ToolResult.ok("written")

    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=[_CALL])],
        _tool(handler=crashing, effect_kind="idempotent", reconciliation="retry"),
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1"))
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()

    resumed, _, engine = await _resume(
        tmp_path, _tool(effect_kind="idempotent", reconciliation="retry")
    )

    assert "may be called again safely" in _last_output(engine).payload["output"]
    assert [c.as_dict() for c in captured[-1]] == [
        {"call_id": "call-1", "name": "remote_write", "disposition": "safe_to_retry"}
    ]
    await resumed.close()
    (recovery,) = _recovery(await _load(tmp_path))
    assert (recovery.payload["basis"], recovery.payload["verdict"]) == (
        "effect_kind", "retry_safe",
    )


@pytest.mark.asyncio
async def test_resume_non_idempotent_without_resolver_refused_before_takeover(
    tmp_path: Path,
) -> None:
    """无回查、非幂等、无人可问：只读预检即拒绝，不写接管记录（不抬 epoch）。"""
    _, intent_id = await _crash_mid_tool(tmp_path)
    before = [(e.writer_epoch, e.record_type) for e in await _load(tmp_path)]

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], _tool())
    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="ops-agent",
            resume_thread_id=(await _load(tmp_path))[1].thread_id,
        )

    assert caught.value.code == "audit_resume_recovery_required"
    assert caught.value.record_ids == (intent_id,)
    assert [(e.writer_epoch, e.record_type) for e in await _load(tmp_path)] == before
    await pool.close()


@pytest.mark.asyncio
async def test_resume_operator_resolution_is_recorded_with_operator_actor(
    tmp_path: Path,
) -> None:
    """人的裁决（provide）经 resolver 提交：以 operator actor 落账，结果补写给模型。"""
    _, intent_id = await _crash_mid_tool(tmp_path)
    requests: list[AuditToolOutcomeRequest] = []

    async def resolver(request: AuditToolOutcomeRequest) -> AuditToolOutcomeResolution:
        requests.append(request)
        return AuditToolOutcomeResolution(
            action="provide", operator_id="op-7", output="人工核实：已写入", is_error=False,
        )

    pool, _, engine = await _resume(tmp_path, _tool(), resolver=resolver)

    (request,) = requests
    assert (request.record_id, request.call_id, request.has_recorded_output) == (
        intent_id, "call-1", False,
    )
    assert request.reconcile_status is None
    assert _last_output(engine).payload["output"] == "人工核实：已写入"
    await pool.close()
    (recovery,) = _recovery(await _load(tmp_path))
    assert recovery.actor.kind == "operator"
    assert recovery.actor.principal_id == "op-7"
    assert (recovery.payload["basis"], recovery.payload["verdict"]) == ("operator", "provided")


@pytest.mark.asyncio
async def test_resume_operator_abort_and_second_crash_reads_recovery_as_settled(
    tmp_path: Path,
) -> None:
    """人接受未知（abort）后再次崩溃：冷读新记录类型视为已结算，不再征求裁决。"""
    await _crash_mid_tool(tmp_path)
    asked: list[str] = []

    async def resolver(request: AuditToolOutcomeRequest) -> AuditToolOutcomeResolution:
        asked.append(request.record_id)
        return AuditToolOutcomeResolution(action="abort", operator_id="op-7")

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], _tool(), resolver)
    thread_id = (await _load(tmp_path))[1].thread_id
    engine = await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
    )
    assert _last_output(engine).payload["output"].startswith("tool_outcome_unknown:")
    # 第二次崩溃：写者消失，不写 session_ended
    await core.close()
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()

    again, _, engine = await _resume(tmp_path, _tool(), resolver=resolver)

    assert len(asked) == 1
    assert _last_output(engine).payload["output"].startswith("tool_outcome_unknown:")
    await again.close()
    envelopes = await _load(tmp_path)
    assert len(_recovery(envelopes)) == 1
    assert [e.writer_epoch for e in envelopes if e.record_type == "writer_takeover"] == [2, 3]


@pytest.mark.asyncio
async def test_resume_durable_unknown_outcome_settled_by_not_executed(tmp_path: Path) -> None:
    """已 durable 为 unknown 的 outcome：回查未执行 → 只落结论、不补第二条 output。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def slow(args: dict[str, Any], ctx: object) -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult.ok("late")

    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=[_CALL])],
        _tool(handler=slow, effect_kind="reconcilable", reconciliation="query",
              timeout_seconds=0.05),
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1")) == (
        "turn_failed"
    )
    try:
        await pool.close()
    except AuditSessionReleaseError:
        pass  # 冻结的 Session 收尾无法落 terminal（与崩溃同形）
    outcome = next(
        e for e in await _load(tmp_path) if e.record_type == "tool_outcome_committed"
    )
    assert outcome.payload["status"] == "unknown"

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        return ReconcileVerdict(status="not_executed")

    resumed, _, engine = await _resume(
        tmp_path, _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile)
    )

    outputs = [i for i in engine.history_snapshot() if i.kind == "function_call_output"]
    assert len(outputs) == 1  # 只有 live 写下的那条，不补第二条
    await resumed.close()
    (recovery,) = _recovery(await _load(tmp_path))
    assert recovery.payload["outcome_record_id"] == outcome.record_id
    assert recovery.payload["output"] is None


@pytest.mark.asyncio
async def test_resume_provide_for_recorded_outcome_is_invalid(tmp_path: Path) -> None:
    """模型已看到结果的调用不能 provide 第二条结果：resolution_invalid，不落账。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def slow(args: dict[str, Any], ctx: object) -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult.ok("late")

    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=[_CALL])],
        _tool(handler=slow, timeout_seconds=0.05),
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1"))
    try:
        await pool.close()
    except AuditSessionReleaseError:
        pass

    async def resolver(request: AuditToolOutcomeRequest) -> AuditToolOutcomeResolution:
        assert request.has_recorded_output is True
        return AuditToolOutcomeResolution(action="provide", operator_id="op", output="x")

    fresh = JsonlSessionJournalCore(tmp_path / "journal")
    resumed, _ = await _pool(tmp_path, fresh, [], _tool(), resolver)
    with pytest.raises(AuditResumeError) as caught:
        await resumed.get_or_create(
            session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=engine.thread_id
        )

    assert caught.value.code == "audit_resume_resolution_invalid"
    assert _recovery(await _load(tmp_path)) == []
    await resumed.close()


@pytest.mark.asyncio
async def test_resume_resolver_exception_propagates_and_releases_lease(tmp_path: Path) -> None:
    """resolver 自身抛错：原样上抛、不落恢复记录，写者锁已释放（可再次接管）。"""
    await _crash_mid_tool(tmp_path)

    async def resolver(request: AuditToolOutcomeRequest) -> AuditToolOutcomeResolution:
        raise LookupError("decision store down")

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], _tool(), resolver)
    with pytest.raises(LookupError):
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="ops-agent",
            resume_thread_id=(await _load(tmp_path))[1].thread_id,
        )

    assert _recovery(await _load(tmp_path)) == []
    probe = JsonlSessionJournalCore(tmp_path / "journal")
    await probe.open_existing(_SESSION, writer_id="probe", operation_id="probe")
    await probe.close()
    await pool.close()


@pytest.mark.asyncio
async def test_resume_resolver_returning_none_keeps_refusing(tmp_path: Path) -> None:
    """resolver 尚无裁决（None）：仍按 recovery_required 拒绝并列出该调用。"""
    _, intent_id = await _crash_mid_tool(tmp_path)

    async def resolver(request: AuditToolOutcomeRequest) -> None:
        return None

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], _tool(), resolver)
    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="ops-agent",
            resume_thread_id=(await _load(tmp_path))[1].thread_id,
        )

    assert (caught.value.code, caught.value.record_ids) == (
        "audit_resume_recovery_required", (intent_id,),
    )
    await pool.close()


def test_split_unsettled_keeps_child_thread_and_orphan_outcome_as_others() -> None:
    """子 thread 的调用、引用缺失的 unknown outcome 不走工具恢复，仍一律 fail closed。"""
    from datetime import UTC, datetime

    from taifeng.conversation.journal.framing import encode_batch
    from taifeng.conversation.journal.models import ActorRef, JournalRecord
    from taifeng.loop.audit_resume_tools import split_unsettled

    def record(record_id: str, record_type: str, payload: dict[str, Any],
               thread_id: str) -> JournalRecord:
        return JournalRecord(
            session_id="ses", record_id=record_id, record_type=record_type,
            actor=ActorRef(kind="system", source="test"), payload=payload,
            operation_id="thr_child:sub:turn:0:tool:c1", thread_id=thread_id,
        )

    intent_payload = {
        "payload_version": 1, "turn_index": 0, "iteration": 0, "call_id": "c1",
        "name": "remote_write", "arguments_raw": "{}", "effective_arguments": {},
        "parallel_safe": False, "effect_kind": "external_non_idempotent",
        "reconciliation": "manual",
    }
    outcome_payload = {
        "payload_version": 1, "intent_record_id": "missing", "call_id": "c2",
        "name": "remote_write", "status": "unknown", "output": "", "data": {},
        "duration_ms": 1.0,
    }
    envelopes = encode_batch(
        (
            record("child_intent", "tool_intent_committed", intent_payload, "thr_child"),
            record("orphan", "tool_outcome_committed", outcome_payload, "thr_root"),
        ),
        batch_id="b", expected_seq=0, writer_epoch=1, previous_hash="0" * 64,
        recorded_at=datetime(2026, 9, 29, tzinfo=UTC),
    ).envelopes

    calls, others = split_unsettled(envelopes, ("child_intent", "orphan"), "thr_root")

    assert calls == ()
    assert others == ("child_intent", "orphan")
