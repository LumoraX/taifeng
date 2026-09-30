"""审计模式下的挂起与恢复（ADR 0097）：turn 停下等人，``Resume`` 带着答复继续。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.records import ToolStatus
from taifeng.conversation.journal.suspension_records import (
    ResumeAcceptedV1,
    ResumeAppliedV1,
    SessionDetachedV1,
    SuspensionResolvedV1,
    TurnSuspendedV1,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import (
    AuditConfig,
    AuditStaticInputs,
    _validate_unsupported_fields,
)
from taifeng.loop.audit_resume_scan import (
    active_suspensions,
    awaited_intent_ids,
    find_unsettled_effects,
)
from taifeng.loop.audit_suspension import (
    AWAITED_INTENTS_KEY,
    AuditedResumeRejectedError,
    _request_for,
    awaited_convergence,
)
from taifeng.loop.submission import CancelTurn, Resume
from taifeng.loop.turn_helpers import _history_orphan_call_ids
from taifeng.permission import (
    PermissionPolicy,
    PermissionRequest,
    SuspendingPrompter,
)
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.signal import SuspendSignal
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope, JournalRecord

_SESSION = "ses-suspension"

_SKILL = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [guarded, ask_user, plain, sneaky]
max_call_depth: 2
---
# 入口
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True, exist_ok=True)
    (root / "entry" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return root


async def _drive(engine: Any, op: Any, *, deadline_seconds: float = 10.0) -> list[Any]:
    """提交 ``op``，收集到根 turn 停下（完成 / 失败 / 挂起）为止。"""
    events: list[Any] = []
    holder: list[str] = []
    done = asyncio.Event()

    async def collector() -> None:
        async for ev in engine.subscribe_all():
            if not holder or ev.submission_id != holder[0]:
                continue
            events.append(ev)
            kind = ev.msg.kind
            if kind == "turn_suspended" or (
                kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root")
            ):
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)
    holder.append(await engine.submit(op))
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return events


def _call(name: str, call_id: str, **arguments: Any) -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


class _Run:
    """一个审计 Session：带挂起式审批与一个会发问的工具。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.executed: list[str] = []
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.sim: SimClient
        self.core: JsonlSessionJournalCore

    def _tools(self) -> list[ToolSpec]:
        async def guarded(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            decision = await ctx.extras["permission_policy"].check(
                PermissionRequest.for_tool_call(
                    "guarded", args, thread_id=ctx.thread_id,
                    submission_id=str(ctx.extras.get("submission_id") or ""),
                    entry_skill_id="entry", turn_index=int(ctx.extras.get("turn_index") or 0),
                    call_chain=("entry",), extra_metadata={"call_id": ctx.call_id},
                    reason="需要写入",
                )
            )
            if not decision.granted:
                return ToolResult.error(f"permission_denied: {decision.reason}",
                                        reason="permission_denied")
            self.executed.append(f"guarded:{args.get('key')}")
            return ToolResult.ok(f"written {args.get('key')}")

        async def ask(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            raise SuspendSignal(PendingRequest(
                request_id=ctx.call_id, reason=SuspendReason.DATA,
                payload_schema={"type": "object"}, related_call_id=ctx.call_id,
                detail={"prompt": args.get("prompt", "")},
            ))

        async def plain(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            self.executed.append("plain")
            return ToolResult.ok("plain done")

        def spec(name: str, handler: Any, **kwargs: Any) -> ToolSpec:
            return ToolSpec(
                name=name, description=name,
                input_schema={"type": "object", "properties": {}},
                handler=handler, effect_kind="pure", reconciliation="none", **kwargs,
            )

        return [
            spec("guarded", guarded),
            spec("ask_user", ask, can_suspend=True),
            spec("plain", plain, parallel_safe=True),
            # 没有声明可挂起却自行发问：能力违约
            spec("sneaky", ask),
        ]

    async def start(
        self,
        turns: list[SimTurn],
        *,
        resume_thread_id: str | None = None,
        core: JsonlSessionJournalCore | None = None,
    ) -> None:
        self.sim = SimClient(turns=turns)
        self.core = core or JsonlSessionJournalCore(self.tmp_path / "journal")
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(self.tmp_path),
            threads_dir=self.tmp_path / "threads",
            model_client=AttemptObservableClientAdapter(
                self.sim, provider="sim", default_model="sim-model"
            ),
            compressors=[],
            extra_tools=self._tools(),
            permission_policy=PermissionPolicy(
                default_mode="ask", prompter=SuspendingPrompter(),
            ),
            max_parallel_tool_calls=3,
            audit=AuditConfig(
                journal_core=self.core, writer_id="writer-suspension",
                max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
            ),
        )
        self.engine = await self.pool.get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )

    async def ask(self, text: str = "开始") -> list[Any]:
        return await _drive(self.engine, taifeng.UserMessage(text=text))

    def pending(self) -> dict[str, Any]:
        """调用 id → 待答请求。"""
        record = self.engine._find_active_suspension()  # noqa: SLF001
        assert record is not None
        return {p.related_call_id: p for p in record.pending}

    async def resume(self, **answers: Any) -> list[Any]:
        """答复：调用 id → 答复内容。"""
        pending = self.pending()
        return await _drive(self.engine, Resume(
            thread_id=self.engine.thread_id,
            resolutions={pending[call_id].request_id: answer
                         for call_id, answer in answers.items()},
        ))

    async def journal(self) -> list[JournalEnvelope]:
        core = JsonlSessionJournalCore(self.tmp_path / "journal")
        return [envelope async for envelope in core.load(_SESSION)]

    async def projection(self) -> list[Any]:
        raw = [i async for i in await self.pool.store.load_thread(self.engine.thread_id)]
        return reconstruct_logical_history(raw)

    def kinds(self) -> list[str]:
        return [item.kind for item in self.engine.history_snapshot()]


def _of(envelopes: list[JournalEnvelope], record_type: str) -> list[JournalEnvelope]:
    return [e for e in envelopes if e.record_type == record_type]


def _types_after(envelopes: list[JournalEnvelope], record_type: str) -> list[str]:
    """从某类记录第一次出现起的记录类型序列。"""
    start = next(i for i, e in enumerate(envelopes) if e.record_type == record_type)
    return [e.record_type for e in envelopes[start:]]


_GUARDED = [
    SimTurn(text="申请写入", tool_calls=[_call("guarded", "c1", key="k1")]),
    SimTurn(text="完成"),
]


async def _suspend(run: _Run, turns: list[SimTurn] | None = None) -> list[Any]:
    await run.start(turns or list(_GUARDED))
    events = await run.ask()
    assert events[-1].msg.kind == "turn_suspended", events[-1].msg.data
    return events


# ====================================================================
# 静态门
# ====================================================================


def test_suspending_approval_and_suspending_tools_are_admitted(tmp_path: Path) -> None:
    run = _Run(tmp_path)

    _validate_unsupported_fields(AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False, skill_suspension_enabled=False,
        permission_policy=PermissionPolicy(prompter=SuspendingPrompter()),
        tools=tuple(run._tools()),  # noqa: SLF001
    ))


# ====================================================================
# 挂起
# ====================================================================


async def test_suspension_is_journaled_and_the_call_stays_open(tmp_path: Path) -> None:
    run = _Run(tmp_path)

    await _suspend(run)

    assert run.executed == []
    envelopes = await run.journal()
    (intent,) = _of(envelopes, "tool_intent_committed")
    assert _of(envelopes, "tool_outcome_committed") == []
    (suspended,) = _of(envelopes, "turn_suspended")
    payload = TurnSuspendedV1.model_validate(suspended.payload)
    (awaited,) = payload.awaited
    assert (awaited.reason, awaited.call_id) == ("permission", "c1")
    assert awaited.intent_record_id == intent.record_id
    assert suspended.operation_id.endswith(":turn:0:suspension:0")
    # 断点条目与挂起记录同批、相邻
    item = envelopes[envelopes.index(suspended) + 1]
    assert (item.record_type, item.payload["item_kind"]) == ("conversation_item", "suspension")
    assert item.payload["item_id"] == payload.item_id
    assert item.payload["payload"]["record_id"] == payload.suspension_id
    assert run.kinds() == ["user_message", "assistant_message", "function_call", "suspension"]
    assert await run.projection() == list(run.engine.history_snapshot())
    # 恢复扫描：在等人作答的调用不算未结算
    assert [s.suspension_id for s in active_suspensions(envelopes)] == [payload.suspension_id]
    assert awaited_intent_ids(envelopes) == {intent.record_id}
    assert find_unsettled_effects(envelopes) == ()
    await run.pool.close()


async def test_mixed_batch_settles_what_it_can(tmp_path: Path) -> None:
    """同一批里一个调用完成、一个停下等人：完成的照常结算，等人的保持未结算。"""
    run = _Run(tmp_path)

    await _suspend(run, [
        SimTurn(text="两件事", tool_calls=[
            _call("plain", "p1"), _call("guarded", "c1", key="k1"),
        ]),
        SimTurn(text="完成"),
    ])

    envelopes = await run.journal()
    assert len(_of(envelopes, "tool_intent_committed")) == 2
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["call_id"] == "p1"
    (suspended,) = _of(envelopes, "turn_suspended")
    assert [a["call_id"] for a in suspended.payload["awaited"]] == ["c1"]
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == {"c1"}
    await run.pool.close()


async def test_undeclared_tool_that_asks_freezes_the_session(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([
        SimTurn(text="偷偷发问", tool_calls=[_call("sneaky", "s1", prompt="?")]),
    ])

    events = await run.ask()

    assert events[-1].msg.kind == "turn_failed"
    assert _of(await run.journal(), "turn_suspended") == []
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()


# ====================================================================
# 恢复
# ====================================================================


async def test_approval_reruns_the_call_under_its_original_identity(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run)

    events = await run.resume(c1={"granted": True, "reason": "值班同意"})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert run.executed == ["guarded:k1"]
    envelopes = await run.journal()
    assert _types_after(envelopes, "resume_accepted") == [
        "resume_accepted",
        "suspension_resolved", "conversation_item",
        "resume_applied",
        "permission_decided",
        "tool_outcome_committed", "conversation_item",
        "llm_request_committed", "llm_response_checkpoint",
        "llm_response_committed", "conversation_item",
    ]
    (accepted,) = _of(envelopes, "resume_accepted")
    (suspended,) = _of(envelopes, "turn_suspended")
    request = ResumeAcceptedV1.model_validate(accepted.payload)
    assert request.suspended_record_id == suspended.record_id
    assert request.turn_index == 1
    assert list(request.resolutions.values()) == [{"granted": True, "reason": "值班同意"}]
    # 结果记在原来的调用名下，回指原来的意图
    (intent,) = _of(envelopes, "tool_intent_committed")
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.operation_id == intent.operation_id
    assert outcome.payload["intent_record_id"] == intent.record_id
    assert (outcome.payload["status"], outcome.payload["output"]) == ("success", "written k1")
    # 重跑时的权限裁决落在续跑的 turn 名下
    (decided,) = _of(envelopes, "permission_decided")
    assert decided.payload["decision_reason"] == "resume_preapproved"
    assert decided.operation_id.startswith(f"{run.engine.thread_id}:{accepted.submission_id}:turn:1:")
    resolved = SuspensionResolvedV1.model_validate(_of(envelopes, "suspension_resolved")[0].payload)
    assert [(r.call_id, r.disposition, r.outcome_record_id) for r in resolved.resolved] == [
        ("c1", "approved", None),
    ]
    applied = ResumeAppliedV1.model_validate(_of(envelopes, "resume_applied")[0].payload)
    assert (applied.result_status, applied.rejection_reason) == ("resumed", None)
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    assert await run.projection() == list(run.engine.history_snapshot())
    assert run.sim.ledger.function_call_output_text("c1") == "written k1"
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_resumed_outputs_are_grouped_with_the_sampling_that_made_the_call(
    tmp_path: Path,
) -> None:
    """Responses 协议：恢复后结算的结果带上发出该调用的采样 id，与当场结算的结果形状一致。"""
    run = _Run(tmp_path)
    await _suspend(run)
    state = run.engine._audit_state  # noqa: SLF001
    assert state is not None
    history = [
        item.model_copy(update={"metadata": {**item.metadata, "llm_sample_id": "sample-7"}})
        if item.kind == "function_call" else item
        for item in run.engine.history_snapshot()
    ]
    (suspension,) = [i for i in history if i.kind == "suspension"]
    intents = dict(suspension.metadata[AWAITED_INTENTS_KEY])

    convergence = awaited_convergence(
        state, intents, [_request_for(history, "c1")],
        registry=run.engine._tool_runtime._registry,  # noqa: SLF001
        cancel=run.engine._root_cancel, history=history,  # noqa: SLF001
    )
    item, _ = await convergence.settle(
        _request_for(history, "c1"), ToolResult.ok("written"), ToolStatus.SUCCESS,
    )

    assert item.metadata["origin_llm_sample_id"] == "sample-7"
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()


async def test_denial_settles_the_call_without_running_it(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run)

    events = await run.resume(c1={"granted": False, "reason": "冻结期"})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert run.executed == []
    envelopes = await run.journal()
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "rejected"
    assert "冻结期" in outcome.payload["output"]
    resolved = SuspensionResolvedV1.model_validate(_of(envelopes, "suspension_resolved")[0].payload)
    assert [(r.disposition, r.outcome_record_id) for r in resolved.resolved] == [
        ("denied", outcome.record_id),
    ]
    # 被拒的调用先结算，挂起才结清
    assert outcome.seq < _of(envelopes, "suspension_resolved")[0].seq
    assert "冻结期" in (run.sim.ledger.function_call_output_text("c1") or "")
    assert await run.projection() == list(run.engine.history_snapshot())
    await run.pool.close()


async def test_answer_becomes_the_result_of_the_asking_tool(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run, [
        SimTurn(text="发问", tool_calls=[_call("ask_user", "q1", prompt="预算多少")]),
        SimTurn(text="知道了"),
    ])
    (suspended,) = _of(await run.journal(), "turn_suspended")
    assert suspended.payload["awaited"][0]["reason"] == "data"

    events = await run.resume(q1={"budget": 3000})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await run.journal()
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "success"
    assert json.loads(outcome.payload["output"]) == {"budget": 3000}
    resolved = SuspensionResolvedV1.model_validate(_of(envelopes, "suspension_resolved")[0].payload)
    assert resolved.resolved[0].disposition == "answered"
    assert json.loads(run.sim.ledger.function_call_output_text("q1") or "{}") == {"budget": 3000}
    await run.pool.close()


async def test_two_awaited_calls_are_resolved_together(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run, [
        SimTurn(text="两个申请", tool_calls=[
            _call("guarded", "c1", key="k1"), _call("guarded", "c2", key="k2"),
        ]),
        SimTurn(text="完成"),
    ])
    (suspended,) = _of(await run.journal(), "turn_suspended")
    assert [a["call_id"] for a in suspended.payload["awaited"]] == ["c1", "c2"]

    events = await run.resume(c1={"granted": True}, c2={"granted": False, "reason": "不需要"})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert run.executed == ["guarded:k1"]
    envelopes = await run.journal()
    statuses = {e.payload["call_id"]: e.payload["status"]
                for e in _of(envelopes, "tool_outcome_committed")}
    assert statuses == {"c1": "success", "c2": "rejected"}
    resolved = SuspensionResolvedV1.model_validate(_of(envelopes, "suspension_resolved")[0].payload)
    assert [(r.call_id, r.disposition) for r in resolved.resolved] == [
        ("c1", "approved"), ("c2", "denied"),
    ]
    assert _history_orphan_call_ids(list(run.engine.history_snapshot())) == set()
    await run.pool.close()


# ====================================================================
# 被拒的 Resume
# ====================================================================


async def _rejected(run: _Run, op: Resume) -> str:
    with pytest.raises(AuditedResumeRejectedError) as raised:
        await run.engine.submit(op)
    return raised.value.reason


async def test_inapplicable_resume_is_rejected_durably(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run, [
        SimTurn(text="两个申请", tool_calls=[
            _call("guarded", "c1", key="k1"), _call("guarded", "c2", key="k2"),
        ]),
        SimTurn(text="完成"),
    ])
    pending = run.pending()
    thread = run.engine.thread_id
    every = {p.request_id: {"granted": True} for p in pending.values()}
    before = list(run.engine.history_snapshot())

    reasons = [
        await _rejected(run, Resume(
            thread_id=thread, resolutions={pending["c1"].request_id: {"granted": True}},
        )),
        await _rejected(run, Resume(thread_id="thr_other", resolutions=every)),
        await _rejected(run, Resume(thread_id=thread, resolutions={**every, "req_x": {}})),
        await _rejected(run, Resume(
            thread_id=thread, resolutions=dict.fromkeys(every, "yes"),
        )),
        await _rejected(run, Resume(
            thread_id=thread, resolutions=dict.fromkeys(every, {"granted": True, "x": object()}),
        )),
    ]

    assert reasons == [
        "resume_must_resolve_every_request",
        "resume_thread_not_root",
        "resume_must_resolve_every_request",
        "invalid_payload_shape",
        "resume_resolutions_not_canonical",
    ]
    envelopes = await run.journal()
    rejected = _of(envelopes, "submission_rejected")
    assert [e.payload["stable_error"]["code"] for e in rejected] == reasons
    assert all(e.payload["op_kind"] == "resume" for e in rejected)
    # 什么都没有被处置
    assert _of(envelopes, "resume_accepted") == []
    assert list(run.engine.history_snapshot()) == before
    assert len(run.pending()) == 2
    await run.pool.close()


async def test_resume_without_a_suspension_is_rejected(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([SimTurn(text="你好")])
    await run.ask()

    reason = await _rejected(run, Resume(
        thread_id=run.engine.thread_id, resolutions={"req_1": {"granted": True}},
    ))

    assert reason == "no_active_suspension"
    await run.pool.close()


# ====================================================================
# 释放、崩溃与接管
# ====================================================================


async def test_release_while_waiting_detaches_instead_of_ending(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run)
    thread_id = run.engine.thread_id
    before = list(run.engine.history_snapshot())

    await run.pool.close()

    envelopes = await run.journal()
    assert _of(envelopes, "session_ended") == []
    assert _of(envelopes, "thread_terminal") == []
    (detached,) = _of(envelopes, "session_detached")
    payload = SessionDetachedV1.model_validate(detached.payload)
    assert payload.reason == "awaiting_resume"
    assert payload.suspension_ids == (_of(envelopes, "turn_suspended")[0].payload["suspension_id"],)

    resumed = _Run(tmp_path)
    resumed.executed = run.executed
    await resumed.start([SimTurn(text="完成")], resume_thread_id=thread_id)

    assert list(resumed.engine.history_snapshot()) == before
    events = await resumed.resume(c1={"granted": True})
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert resumed.executed == ["guarded:k1"]
    await resumed.pool.close()
    final = await resumed.journal()
    # 这一次是真的结束了
    assert len(_of(final, "session_ended")) == 1
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_crash_while_waiting_can_be_taken_over(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await _suspend(run)
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start([SimTurn(text="完成")], resume_thread_id=thread_id)
    events = await resumed.resume(c1={"granted": False, "reason": "过期"})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await resumed.journal()
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "rejected"
    # 续跑的 turn 序号接着 Journal 里已有的往下编
    assert ResumeAcceptedV1.model_validate(
        _of(envelopes, "resume_accepted")[0].payload
    ).turn_index == 1
    await resumed.pool.close()


class _CrashOnResolved(JsonlSessionJournalCore):
    """在结清记录落账前「进程死亡」的 core。"""

    async def append_batch(self, records: Sequence[JournalRecord], **kwargs: Any) -> Any:
        if any(record.record_type == "suspension_resolved" for record in records):
            await self.close()
        return await super().append_batch(records, **kwargs)


async def test_resume_interrupted_halfway_can_be_submitted_again(tmp_path: Path) -> None:
    """答复处置到一半崩溃（被拒的调用已结算、挂起未结清）：接管后重新提交，不重复结算。"""
    run = _Run(tmp_path)
    await run.start(list(_GUARDED), core=_CrashOnResolved(tmp_path / "journal"))
    await run.ask()
    thread_id = run.engine.thread_id

    await run.resume(c1={"granted": False, "reason": "冻结期"})

    envelopes = await run.journal()
    assert len(_of(envelopes, "resume_accepted")) == 1
    assert len(_of(envelopes, "tool_outcome_committed")) == 1
    assert _of(envelopes, "suspension_resolved") == []
    assert [s.suspension_id for s in active_suspensions(envelopes)] != []
    assert find_unsettled_effects(envelopes) == ()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start([SimTurn(text="换个办法")], resume_thread_id=thread_id)
    events = await resumed.resume(c1={"granted": False, "reason": "冻结期"})

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    final = await resumed.journal()
    assert len(_of(final, "tool_outcome_committed")) == 1
    assert len(_of(final, "suspension_resolved")) == 1
    assert len(_of(final, "resume_accepted")) == 2
    kinds = [item.kind for item in resumed.engine.history_snapshot()]
    assert kinds.count("function_call_output") == 1
    assert _history_orphan_call_ids(list(resumed.engine.history_snapshot())) == set()
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_cancel_turn_does_not_discard_a_journaled_suspension(tmp_path: Path) -> None:
    """对挂起中的 turn 发 CancelTurn：没有在跑的目标可取消，挂起原样保留，仍可作答。"""
    run = _Run(tmp_path)
    events = await _suspend(run)
    suspended_submission = events[-1].submission_id

    await run.engine.submit(CancelTurn(submission_id=suspended_submission))
    await asyncio.sleep(0.05)

    assert list(run.pending()) == ["c1"]
    envelopes = await run.journal()
    assert _of(envelopes, "suspension_resolved") == []
    assert [s.suspension_id for s in active_suspensions(envelopes)] != []
    resumed = await run.resume(c1={"granted": False, "reason": "放弃"})
    assert resumed[-1].msg.kind == "turn_completed", resumed[-1].msg.data
    await run.pool.close()
