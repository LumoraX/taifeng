"""strict audit resume：同步 call_skill 的子 thread 在执行途中崩溃（ADR 0076）。

此前子 thread 上的待收敛调用必然伴随父 thread 未结算的 skill 派发，resume 整体 fail closed。
现在自底向上收敛：子 thread 的工具调用按 ADR 0070 / 0075 的规则结算 → 被中断的 skill 派发落
终态 → 父 thread 的 call_skill 调用得到一条说明中断的结果。

「崩溃」的模拟同 ``test_audit_resume_tools``：handler 执行途中 ``core.close()``，或 core 在某类
batch 落账前关闭。
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
from taifeng.loop.audit_resume_resolution import (
    AuditToolOutcomeRequest,
    AuditToolOutcomeResolution,
)
from taifeng.tool.spec import ReconcileVerdict, ToolResult, ToolSpec
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord

_SESSION = "ses-child-recovery"
_PARENT = """---
name: ops-agent
description: 把写入任务派给子技能的入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [writer]
max_call_depth: 3
---
# 入口
把写入任务派给 writer。
"""
_CHILD = """---
name: writer
description: 调用外部写接口
version: 1.0.0
type: composite
entry: false
model: mock-model
tool_names: [remote_write]
max_call_depth: 2
---
# 写入
调用 remote_write 完成写入。
"""
_DISPATCH = {
    "id": "call-skill-1",
    "name": "call_skill",
    "arguments": '{"skill_id": "writer", "reason": "写 k1"}',
}
_WRITE = {"id": "call-write-1", "name": "remote_write", "arguments": '{"key": "k1"}'}


class _CrashingCore(JsonlSessionJournalCore):
    """在首条满足条件的 record 落账前「进程死亡」的 core。"""

    def __init__(self, root: Path, crash_before: Callable[[JournalRecord], bool]) -> None:
        """记录触发崩溃的判据。"""
        super().__init__(root)
        self._crash_before = crash_before

    async def append(self, record: JournalRecord, **kwargs: Any) -> Any:
        """单条追加同样受判据约束。"""
        if self._crash_before(record):
            await self.close()
        return await super().append(record, **kwargs)

    async def append_batch(self, records: Sequence[JournalRecord], **kwargs: Any) -> Any:
        """batch 内任一 record 命中判据即整批写不进去。"""
        if any(self._crash_before(record) for record in records):
            await self.close()
        return await super().append_batch(records, **kwargs)


def _skills(tmp_path: Path) -> Path:
    """写入父入口与可调工具的子 skill（重复调用幂等）。"""
    root = tmp_path / "skills"
    for name, body in (("ops-agent", _PARENT), ("writer", _CHILD)):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
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
            writer_id="writer-child-recovery",
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


async def _run_and_crash(
    tmp_path: Path, core: JsonlSessionJournalCore, tool: ToolSpec,
) -> str:
    """跑「父派发子、子调工具」直到崩溃；返回 root thread id。"""
    pool, _ = await _pool(
        tmp_path,
        core,
        [SimTurn(text="派给 writer", tool_calls=[_DISPATCH]),
         SimTurn(text="写入", tool_calls=[_WRITE])],
        tool,
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1"))
    assert kind == "turn_failed"
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    return engine.thread_id


async def _crash_in_child_tool(tmp_path: Path) -> str:
    """子 thread 的 remote_write 执行途中「进程死亡」。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def crashing(args: dict[str, Any], ctx: object) -> ToolResult:
        await core.close()
        return ToolResult.ok("written")

    thread_id = await _run_and_crash(tmp_path, core, _tool(handler=crashing))
    envelopes = await _load(tmp_path)
    intents = [e for e in envelopes if e.record_type == "tool_intent_committed"]
    assert [e.payload["name"] for e in intents] == ["call_skill", "remote_write"]
    assert intents[1].thread_id != thread_id
    assert not [e for e in envelopes if e.record_type == "tool_outcome_committed"]
    assert not [e for e in envelopes if e.record_type == "skill_dispatch_finished"]
    return thread_id


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


def _of(envelopes: list[JournalEnvelope], record_type: str) -> list[JournalEnvelope]:
    """按类型筛选 record。"""
    return [e for e in envelopes if e.record_type == record_type]


def _root_output(engine: taifeng.AgentEngine) -> taifeng.ResponseItem:
    """root history 末项必须是补写给 call_skill 的结果。"""
    item = engine.history_snapshot()[-1]
    assert item.kind == "function_call_output"
    assert item.payload["call_id"] == "call-skill-1"
    return item


@pytest.mark.asyncio
async def test_child_tool_reconciled_then_dispatch_and_parent_call_settled(
    tmp_path: Path,
) -> None:
    """子 thread 工具可回查：自底向上三层结论同批落账，root 续跑时配对完整。"""
    root_thread = await _crash_in_child_tool(tmp_path)

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        assert (arguments, call_id) == ({"key": "k1"}, "call-write-1")
        return ReconcileVerdict(status="completed", output="k1 已写入 rev=7")

    pool, sim, engine = await _resume(
        tmp_path,
        _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile),
        turns=[SimTurn(text="已了解")],
    )

    output = _root_output(engine)
    assert output.payload["is_error"] is True
    text = output.payload["output"]
    assert text.startswith("skill_dispatch_interrupted:")
    assert "writer" in text
    # 模型据此知道子技能里那次写入其实已经完成
    assert "remote_write" in text
    assert "reconciled" in text
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="继续")) == (
        "turn_completed"
    )
    assert sim.ledger.function_call_output_text("call-skill-1") == text
    await pool.close()

    envelopes = await _load(tmp_path)
    child_thread = _of(envelopes, "skill_dispatch_started")[0].payload["child_thread_id"]
    recoveries = _of(envelopes, "tool_recovery_committed")
    assert [(r.thread_id, r.payload["basis"], r.payload["verdict"]) for r in recoveries] == [
        (child_thread, "reconcile", "completed"),
        (root_thread, "dispatch", "interrupted"),
    ]
    finished = _of(envelopes, "skill_dispatch_finished")
    assert len(finished) == 1
    assert finished[0].payload["status"] == "cancelled"
    assert finished[0].payload["end_reason"] == "process_recovery"
    assert finished[0].payload["child_thread_id"] == child_thread
    assert finished[0].actor.kind == "system"
    assert finished[0].actor.source == "recovery"
    terminals = [e for e in _of(envelopes, "thread_terminal") if e.thread_id == child_thread]
    assert [t.payload["status"] for t in terminals] == ["cancelled"]
    # 因果顺序：子调用结论 → 派发终态 → 父调用结论
    seq = {e.record_id: e.seq for e in envelopes}
    assert seq[recoveries[0].record_id] < seq[finished[0].record_id]
    assert seq[finished[0].record_id] < seq[recoveries[1].record_id]
    # 被中断的执行不记战绩
    outcomes = [
        e for e in _of(envelopes, "conversation_item")
        if e.payload.get("item_kind") == "skill_outcome"
    ]
    assert outcomes == []


@pytest.mark.asyncio
async def test_child_thread_gets_its_own_recovered_output(tmp_path: Path) -> None:
    """子 thread 的调用在子 thread 上配对完整（Journal 内）。"""
    await _crash_in_child_tool(tmp_path)

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        return ReconcileVerdict(status="not_executed")

    pool, _, _ = await _resume(
        tmp_path, _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile),
    )
    envelopes = await _load(tmp_path)
    child_thread = _of(envelopes, "skill_dispatch_started")[0].payload["child_thread_id"]
    # 子 thread 的 transcript 投影同样补齐了恢复写入的结果
    projected = [
        item async for item in await pool._store.load_thread(child_thread)  # noqa: SLF001
    ]
    assert [i.payload["call_id"] for i in projected if i.kind == "function_call_output"] == [
        "call-write-1"
    ]
    await pool.close()
    child_outputs = [
        e for e in _of(envelopes, "conversation_item")
        if e.thread_id == child_thread
        and e.payload.get("item_kind") == "function_call_output"
    ]
    assert len(child_outputs) == 1
    assert child_outputs[0].payload["payload"]["call_id"] == "call-write-1"
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


@pytest.mark.asyncio
async def test_child_non_idempotent_tool_without_resolver_refused_in_precheck(
    tmp_path: Path,
) -> None:
    """子 thread 的调用只能交人而无人可问：预检即拒，只列出该调用，不写接管记录。"""
    await _crash_in_child_tool(tmp_path)
    before = await _load(tmp_path)
    child_intent = _of(before, "tool_intent_committed")[1]

    with pytest.raises(AuditResumeError) as raised:
        await _resume(tmp_path, _tool())

    assert raised.value.code == "audit_resume_recovery_required"
    assert raised.value.record_ids == (child_intent.record_id,)
    after = await _load(tmp_path)
    assert [e.record_id for e in after] == [e.record_id for e in before]


@pytest.mark.asyncio
async def test_child_tool_resolved_by_operator(tmp_path: Path) -> None:
    """子 thread 的调用交人裁决：请求带子 thread id，裁决以 operator actor 落账。"""
    root_thread = await _crash_in_child_tool(tmp_path)
    asked: list[AuditToolOutcomeRequest] = []

    async def resolver(request: AuditToolOutcomeRequest) -> AuditToolOutcomeResolution:
        asked.append(request)
        return AuditToolOutcomeResolution(
            action="provide", operator_id="op-7", output="k1 已人工确认写入", is_error=False,
        )

    pool, _, engine = await _resume(tmp_path, _tool(), resolver=resolver)

    assert [r.name for r in asked] == ["remote_write"]
    assert asked[0].thread_id != root_thread
    assert "operator_resolved" in _root_output(engine).payload["output"]
    await pool.close()
    recoveries = _of(await _load(tmp_path), "tool_recovery_committed")
    assert [r.actor.kind for r in recoveries] == ["operator", "system"]


@pytest.mark.asyncio
async def test_dispatch_selected_but_never_started(tmp_path: Path) -> None:
    """崩溃在 skill_selected 与 started 之间：子 thread 从未创建，派发判未启动。"""
    core = _CrashingCore(
        tmp_path / "journal", lambda r: r.record_type == "skill_dispatch_started"
    )
    await _run_and_crash(tmp_path, core, _tool())
    before = await _load(tmp_path)
    assert len(_of(before, "skill_selected")) == 1
    assert not _of(before, "skill_dispatch_started")

    pool, _, engine = await _resume(tmp_path, _tool(), turns=[SimTurn(text="好")])

    text = _root_output(engine).payload["output"]
    assert text.startswith("not_executed:")
    await pool.close()
    after = await _load(tmp_path)
    finished = _of(after, "skill_dispatch_finished")
    assert [f.payload["status"] for f in finished] == ["rejected"]
    assert finished[0].payload["end_reason"] == "process_recovery_before_start"
    assert finished[0].payload["child_thread_id"] is None
    recovery = _of(after, "tool_recovery_committed")
    assert [(r.payload["basis"], r.payload["verdict"]) for r in recovery] == [
        ("dispatch", "not_started"),
    ]


@pytest.mark.asyncio
async def test_call_skill_intent_without_selection(tmp_path: Path) -> None:
    """崩溃在 call_skill 意图与 skill_selected 之间：派发从未开始，不需要人裁决。"""
    core = _CrashingCore(tmp_path / "journal", lambda r: r.record_type == "skill_selected")
    await _run_and_crash(tmp_path, core, _tool())
    before = await _load(tmp_path)
    assert len(_of(before, "tool_intent_committed")) == 1
    assert not _of(before, "skill_selected")

    pool, _, engine = await _resume(tmp_path, _tool(), turns=[SimTurn(text="好")])

    assert _root_output(engine).payload["output"].startswith("not_executed:")
    await pool.close()
    after = await _load(tmp_path)
    recovery = _of(after, "tool_recovery_committed")
    assert [(r.payload["basis"], r.payload["verdict"]) for r in recovery] == [
        ("dispatch", "not_started"),
    ]
    assert not _of(after, "skill_dispatch_finished")


@pytest.mark.asyncio
async def test_dispatch_finished_but_parent_outcome_missing(tmp_path: Path) -> None:
    """子 skill 已 durable 结束、父调用结果未落账：用已落账的子结果结算父调用。"""
    core = _CrashingCore(
        tmp_path / "journal",
        lambda r: (
            r.record_type == "tool_outcome_committed"
            and r.payload.get("name") == "call_skill"
        ),
    )
    pool, _ = await _pool(
        tmp_path,
        core,
        [SimTurn(text="派给 writer", tool_calls=[_DISPATCH]), SimTurn(text="k1 写入完成")],
        _tool(),
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1")) == (
        "turn_failed"
    )
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    before = await _load(tmp_path)
    assert [f.payload["status"] for f in _of(before, "skill_dispatch_finished")] == ["success"]

    again, _, resumed = await _resume(tmp_path, _tool(), turns=[SimTurn(text="好")])

    output = _root_output(resumed)
    assert output.payload == {
        "call_id": "call-skill-1", "output": "k1 写入完成", "is_error": False,
    }
    await again.close()
    recovery = _of(await _load(tmp_path), "tool_recovery_committed")
    assert [(r.payload["basis"], r.payload["verdict"]) for r in recovery] == [
        ("dispatch", "completed"),
    ]


@pytest.mark.asyncio
async def test_second_resume_reads_dispatch_recovery_as_settled(tmp_path: Path) -> None:
    """收敛后再次崩溃：冷读全部结论视为已结算，不重复回查、不重复落账。"""
    await _crash_in_child_tool(tmp_path)
    calls: list[str] = []

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        calls.append(call_id)
        return ReconcileVerdict(status="not_executed")

    tool = _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile)
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool, _ = await _pool(tmp_path, core, [], tool)
    thread_id = (await _load(tmp_path))[1].thread_id
    await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=thread_id
    )
    await core.close()
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    settled = await _load(tmp_path)

    again, _, engine = await _resume(tmp_path, tool)

    assert calls == ["call-write-1"]
    assert _root_output(engine).payload["output"].startswith("skill_dispatch_interrupted:")
    await again.close()
    after = await _load(tmp_path)
    assert len(_of(after, "tool_recovery_committed")) == len(
        _of(settled, "tool_recovery_committed")
    ) == 2
    assert len(_of(after, "skill_dispatch_finished")) == 1


def test_scope_keeps_child_thread_without_interrupted_dispatch_as_others() -> None:
    """子 thread 上的调用若不属于任何被中断的派发，仍一律 fail closed。"""
    from datetime import UTC, datetime

    from taifeng.conversation.journal.framing import encode_batch
    from taifeng.conversation.journal.models import ActorRef, JournalRecord
    from taifeng.loop.audit_resume_dispatch import build_recovery_scope
    from taifeng.loop.audit_resume_scan import find_unsettled_effects

    intent = JournalRecord(
        session_id="ses",
        record_id="child_intent",
        record_type="tool_intent_committed",
        actor=ActorRef(kind="system", source="test"),
        payload={
            "payload_version": 1, "turn_index": 0, "iteration": 0, "call_id": "c1",
            "name": "remote_write", "arguments_raw": "{}", "effective_arguments": {},
            "parallel_safe": False, "effect_kind": "pure", "reconciliation": "none",
        },
        operation_id="thr_child:sub:turn:0:tool:c1",
        thread_id="thr_child",
    )
    envelopes = encode_batch(
        (intent,), batch_id="b", expected_seq=0, writer_epoch=1,
        previous_hash="0" * 64, recorded_at=datetime(2026, 9, 30, tzinfo=UTC),
    ).envelopes

    scope = build_recovery_scope(
        envelopes, find_unsettled_effects(envelopes), "thr_root"
    )

    assert scope.empty
    assert scope.others == ("child_intent",)
    assert scope.threads == frozenset({"thr_root"})


_MIDDLE = """---
name: coordinator
description: 中间层：把写入再派给 writer
version: 1.0.0
type: composite
entry: false
model: mock-model
child_skills: [writer]
max_call_depth: 3
---
# 中间层
把写入派给 writer。
"""
_NESTED_PARENT = _PARENT.replace("child_skills: [writer]", "child_skills: [coordinator]")


@pytest.mark.asyncio
async def test_nested_dispatch_chain_settles_bottom_up(tmp_path: Path) -> None:
    """两层派发（入口 → 中间层 → writer）在最内层工具执行途中崩溃：逐层收敛。"""
    root = _skills(tmp_path)
    (root / "ops-agent" / "SKILL.md").write_text(_NESTED_PARENT, encoding="utf-8")
    (root / "coordinator").mkdir(parents=True, exist_ok=True)
    (root / "coordinator" / "SKILL.md").write_text(_MIDDLE, encoding="utf-8")
    core = JsonlSessionJournalCore(tmp_path / "journal")

    async def crashing(args: dict[str, Any], ctx: object) -> ToolResult:
        await core.close()
        return ToolResult.ok("written")

    def dispatch(call_id: str, skill_id: str) -> dict[str, str]:
        arguments = f'{{"skill_id": "{skill_id}", "reason": "写 k1"}}'
        return {"id": call_id, "name": "call_skill", "arguments": arguments}

    sim = SimClient(turns=[
        SimTurn(text="派给中间层", tool_calls=[dispatch("call-skill-1", "coordinator")]),
        SimTurn(text="派给 writer", tool_calls=[dispatch("call-skill-2", "writer")]),
        SimTurn(text="写入", tool_calls=[_WRITE]),
    ])

    async def make_pool(
        journal: JsonlSessionJournalCore, client: SimClient, tool: ToolSpec,
    ) -> taifeng.EnginePool:
        return await taifeng.EnginePool.create(
            skills_dir=root,
            threads_dir=tmp_path / "threads",
            model_client=AttemptObservableClientAdapter(
                client, provider="sim", default_model="sim-model"
            ),
            compressors=[],
            extra_tools=[tool],
            audit=AuditConfig(
                journal_core=journal,
                writer_id="writer-child-recovery",
                max_attachment_bytes=65536,
                max_total_attachment_bytes=1048576,
            ),
        )

    pool = await make_pool(core, sim, _tool(handler=crashing))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="ops-agent")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="写 k1")) == (
        "turn_failed"
    )
    with pytest.raises(AuditSessionReleaseError):
        await pool.close()
    assert len(_of(await _load(tmp_path), "skill_dispatch_started")) == 2

    async def reconcile(arguments: dict[str, Any], call_id: str) -> ReconcileVerdict:
        return ReconcileVerdict(status="not_executed")

    again = await make_pool(
        JsonlSessionJournalCore(tmp_path / "journal"),
        SimClient(turns=[]),
        _tool(effect_kind="reconcilable", reconciliation="query", reconcile=reconcile),
    )
    resumed = await again.get_or_create(
        session_id=_SESSION, entry_skill_id="ops-agent", resume_thread_id=engine.thread_id
    )

    text = _root_output(resumed).payload["output"]
    assert text.startswith("skill_dispatch_interrupted:")
    assert "'coordinator'" in text
    # root 只看到直接子层的处置：中间层那次派发本身被中断
    assert "call_skill (call-skill-2): dispatch_interrupted" in text
    await again.close()

    envelopes = await _load(tmp_path)
    recoveries = _of(envelopes, "tool_recovery_committed")
    assert [(r.payload["name"], r.payload["verdict"]) for r in recoveries] == [
        ("remote_write", "not_executed"),
        ("call_skill", "interrupted"),
        ("call_skill", "interrupted"),
    ]
    assert recoveries[1].payload["call_id"] == "call-skill-2"
    assert recoveries[2].payload["call_id"] == "call-skill-1"
    finished = _of(envelopes, "skill_dispatch_finished")
    assert [f.payload["status"] for f in finished] == ["cancelled", "cancelled"]
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
