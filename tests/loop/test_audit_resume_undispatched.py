"""strict audit resume：模型回复已落账、工具意图尚未登记时崩溃的窗口（ADR 0075）。

Journal 的真实形态：``llm_response_committed`` 与 ``function_call`` 会话项已 durable，
``tool_intent_committed`` 一条都没有。intent 先于任何派发落账，所以这些调用**确定没有执行**。

「崩溃」的模拟：core 在即将追加含 ``tool_intent_committed`` 的 batch 时先行 ``close()``——
写者锁释放，意图 batch 写不进去，与进程死在两个 batch 之间时的 Journal 逐字节相同。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.audit_resume import AuditResumeError
from taifeng.loop.audit_resume_scan import find_undispatched_calls
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord

_SESSION = "ses-undispatched"
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
_CALL_1 = {"id": "call-1", "name": "remote_write", "arguments": '{"key": "k1"}'}
_CALL_2 = {"id": "call-2", "name": "remote_write", "arguments": '{"key": "k2"}'}


class _CrashBeforeIntentCore(JsonlSessionJournalCore):
    """在意图 batch 落账前「进程死亡」的 core。"""

    async def append_batch(self, records: Sequence[JournalRecord], **kwargs: Any) -> Any:
        """遇到含工具意图的 batch 先关闭写者，再让追加自然失败。"""
        if any(record.record_type == "tool_intent_committed" for record in records):
            await self.close()
        return await super().append_batch(records, **kwargs)


def _skills(tmp_path: Path) -> Path:
    """写入只开放 remote_write 的入口 skill（重复调用幂等）。"""
    root = tmp_path / "skills"
    (root / "ops-agent").mkdir(parents=True, exist_ok=True)
    (root / "ops-agent" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return root


def _tool(executed: list[dict[str, Any]]) -> ToolSpec:
    """非幂等、无回查的写工具：若走 intent 恢复路径只能交人。"""

    async def handler(args: dict[str, Any], ctx: object) -> ToolResult:
        executed.append(args)
        return ToolResult.ok(f"written {args['key']}")

    return ToolSpec(
        name="remote_write",
        description="写外部系统",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler,
        effect_kind="external_non_idempotent",
        reconciliation="manual",
    )


async def _pool(
    tmp_path: Path,
    core: JsonlSessionJournalCore,
    turns: list[SimTurn],
    tool: ToolSpec,
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
            writer_id="writer-undispatched",
            max_attachment_bytes=65536,
            max_total_attachment_bytes=1048576,
        ),
    )
    return pool, sim


async def _load(tmp_path: Path) -> list[JournalEnvelope]:
    """用独立 core 实例读出全部 committed envelopes。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    return [envelope async for envelope in core.load(_SESSION)]


async def _crash_before_intent(
    tmp_path: Path, calls: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """跑到意图 batch 落账前「进程死亡」；返回 root thread id 与工具执行记录。"""
    executed: list[dict[str, Any]] = []
    core = _CrashBeforeIntentCore(tmp_path / "journal")
    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=calls)], _tool(executed)
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text="写"))
    assert kind == "turn_failed"
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()

    envelopes = await _load(tmp_path)
    assert [e for e in envelopes if e.record_type == "llm_response_committed"]
    assert not [e for e in envelopes if e.record_type == "tool_intent_committed"]
    function_calls = [
        e for e in envelopes
        if e.record_type == "conversation_item"
        and e.payload.get("item_kind") == "function_call"
    ]
    assert len(function_calls) == len(calls)
    return engine.thread_id, executed


async def _resume(
    tmp_path: Path, tool: ToolSpec, turns: list[SimTurn],
) -> tuple[taifeng.EnginePool, SimClient, taifeng.AgentEngine]:
    """以新 core 实例（模拟另一进程）resume 同一 root thread。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, sim = await _pool(tmp_path, core, turns, tool)
    thread_id = (await _load(tmp_path))[1].thread_id
    assert thread_id is not None
    engine = await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
    )
    return pool, sim, engine


def _outputs(engine: taifeng.AgentEngine) -> list[taifeng.ResponseItem]:
    """history 中全部 function_call_output。"""
    return [i for i in engine.history_snapshot() if i.kind == "function_call_output"]


@pytest.mark.asyncio
async def test_resume_settles_undispatched_call_as_not_executed(
    tmp_path: Path,
) -> None:
    """没有 intent 的调用确定未执行：resume 自动补「未执行」结果，不需要人裁决。"""
    _, executed = await _crash_before_intent(tmp_path, [_CALL_1])
    assert executed == []

    resumed_executed: list[dict[str, Any]] = []
    pool, sim, engine = await _resume(
        tmp_path, _tool(resumed_executed), [SimTurn(text="好的")]
    )

    outputs = _outputs(engine)
    assert [o.payload["call_id"] for o in outputs] == ["call-1"]
    assert outputs[0].payload["is_error"] is True
    assert "not_executed" in outputs[0].payload["output"]
    assert outputs[0].metadata["recovered"] is True
    # 恢复从不执行工具
    assert resumed_executed == []
    # 续跑的新 turn 里配对完整，模型看到「未执行」
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="继续")) == (
        "turn_completed"
    )
    assert "not_executed" in sim.ledger.function_call_output_text("call-1")
    await pool.close()


@pytest.mark.asyncio
async def test_resume_settles_every_call_of_parallel_batch(tmp_path: Path) -> None:
    """同一次回复里的多个调用全部收敛，顺序与模型发出的顺序一致。"""
    await _crash_before_intent(tmp_path, [_CALL_1, _CALL_2])

    pool, _, engine = await _resume(tmp_path, _tool([]), [SimTurn(text="好的")])

    assert [o.payload["call_id"] for o in _outputs(engine)] == ["call-1", "call-2"]
    await pool.close()


@pytest.mark.asyncio
async def test_resume_records_undispatched_verdict_durably(tmp_path: Path) -> None:
    """结论 durable 落账，可复核依据；Journal strict verify 通过。"""
    await _crash_before_intent(tmp_path, [_CALL_1])
    pool, _, _ = await _resume(tmp_path, _tool([]), [])
    await pool.close()

    envelopes = await _load(tmp_path)
    recoveries = [e for e in envelopes if e.record_type == "tool_call_undispatched"]
    assert len(recoveries) == 1
    payload = recoveries[0].payload
    assert payload["call_id"] == "call-1"
    assert payload["name"] == "remote_write"
    assert payload["is_error"] is True
    assert recoveries[0].actor.kind == "system"
    # function_call 会话项是结论的因
    function_call = next(
        e for e in envelopes
        if e.record_type == "conversation_item"
        and e.payload.get("item_kind") == "function_call"
    )
    assert payload["function_call_record_id"] == function_call.record_id

    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    assert verification.committed_tail_seq == envelopes[-1].seq
    assert not verification.physical_tail_torn


@pytest.mark.asyncio
async def test_second_resume_does_not_settle_twice(tmp_path: Path) -> None:
    """已收敛的调用在再次 resume 时不重复补写。"""
    await _crash_before_intent(tmp_path, [_CALL_1])
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], _tool([]))
    thread_id = (await _load(tmp_path))[1].thread_id
    await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
    )
    # 第二次崩溃：写者消失，不写 session_ended
    await core.close()
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()

    pool2, _, engine2 = await _resume(tmp_path, _tool([]), [])
    assert len(_outputs(engine2)) == 1
    envelopes = await _load(tmp_path)
    assert len([e for e in envelopes if e.record_type == "tool_call_undispatched"]) == 1
    await pool2.close()


@pytest.mark.asyncio
async def test_resume_refuses_call_id_that_cannot_form_identity(tmp_path: Path) -> None:
    """call id 含分隔符无法构成 operation identity：交人，且预检即拒、不写接管记录。"""
    bad_call = {"id": "call:1", "name": "remote_write", "arguments": "{}"}
    executed: list[dict[str, Any]] = []
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(
        tmp_path, core, [SimTurn(text="写入", tool_calls=[bad_call])], _tool(executed)
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="写")) == (
        "turn_failed"
    )
    await core.close()
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    before = await _load(tmp_path)
    function_call = next(
        e for e in before
        if e.record_type == "conversation_item"
        and e.payload.get("item_kind") == "function_call"
    )

    with pytest.raises(AuditResumeError) as raised:
        await _resume(tmp_path, _tool([]), [])

    assert raised.value.code == "audit_resume_recovery_required"
    assert raised.value.record_ids == (function_call.record_id,)
    assert executed == []
    after = await _load(tmp_path)
    assert [e.record_id for e in after] == [e.record_id for e in before]


@pytest.mark.asyncio
async def test_completed_session_has_no_undispatched_calls(tmp_path: Path) -> None:
    """正常跑完的工具调用不被误判：resume 不写任何恢复记录。"""
    executed: list[dict[str, Any]] = []
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(
        tmp_path,
        core,
        [SimTurn(text="写入", tool_calls=[_CALL_1]), SimTurn(text="完成")],
        _tool(executed),
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="写")) == (
        "turn_completed"
    )
    assert executed == [{"key": "k1"}]
    # 崩溃：写者消失，不写 session_ended
    await core.close()
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    envelopes = await _load(tmp_path)
    assert find_undispatched_calls(envelopes, engine.thread_id) == ()

    again, _, resumed = await _resume(tmp_path, _tool([]), [])

    assert len(_outputs(resumed)) == 1
    await again.close()
    after = await _load(tmp_path)
    assert not [e for e in after if e.record_type == "tool_call_undispatched"]
