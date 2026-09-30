"""审计模式下的分离式派发与 join-barrier（ADR 0098 / 0099）。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.spawn_records import (
    BarrierFiredV1,
    BarrierSettledV1,
    SpawnSettledV1,
    SpawnStartedV1,
)
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import AuditCapabilityError, AuditConfig
from taifeng.loop.audit_resume_scan import find_unsettled_effects
from taifeng.loop.audit_spawn import (
    barrier_thread_id_of,
    barriers_from_journal,
    child_thread_id_of,
    spawn_handles_from_journal,
)
from taifeng.loop.spawn import SpawnRejectedError
from taifeng.permission import (
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.tool.builtins import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_run_in_background_tool,
    make_send_message_tool,
    make_spawn_skill_tool,
    make_wait_any_tool,
    make_wait_peer_tool,
)
from taifeng.tool.builtins.background import BackgroundTaskRegistry
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import run_until_root_done, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope

_SESSION = "ses-spawn"

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [worker, merge]
tool_names: [spawn_skill, join_skill, kill_skill, wait_peer, wait_any, await_skills, send_message, slow, guarded]
max_call_depth: 3
---
ROOT-BODY
"""

_MERGE = """---
name: merge
description: 汇总的
version: 1.0.0
type: composite
model: mock-model
tool_names: [slow]
---
MERGE-BODY
"""

_WORKER = """---
name: worker
description: 干活的
version: 1.0.0
type: composite
model: mock-model
tool_names: [slow, guarded, send_message]
---
WORKER-BODY
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name, body in (("entry", _ENTRY), ("worker", _WORKER), ("merge", _MERGE)):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _call(name: str, call_id: str, **arguments: Any) -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


def _spawn_tools() -> list[ToolSpec]:
    def audited(spec: ToolSpec) -> ToolSpec:
        return spec

    return [audited(make()) for make in (
        make_spawn_skill_tool, make_join_skill_tool, make_kill_skill_tool,
        make_wait_peer_tool, make_wait_any_tool, make_await_skills_tool,
        make_send_message_tool,
    )]


class _Run:
    """一个带分离式派发工具的审计 Session。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.slow_entered = asyncio.Event()
        self.slow_release = asyncio.Event()
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.sim: RoutingSimClient
        self.core: JsonlSessionJournalCore

    def _tools(self) -> list[ToolSpec]:
        async def slow(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            self.slow_entered.set()
            while not self.slow_release.is_set():
                # 工具配合取消：审计模式下每次调用都要在收敛期限内给出确定的结果
                if ctx.cancel.is_cancelled:
                    return ToolResult.error("cancelled", reason="cancelled")
                await asyncio.sleep(0.01)
            return ToolResult.ok("slow done")

        async def guarded(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            decision = await ctx.extras["permission_policy"].check(
                PermissionRequest.for_tool_call(
                    "guarded", args, thread_id=ctx.thread_id,
                    submission_id=str(ctx.extras.get("submission_id") or ""),
                    entry_skill_id="worker", turn_index=0, call_chain=("worker",),
                    extra_metadata={"call_id": ctx.call_id}, reason="需要写入",
                )
            )
            return ToolResult.ok("written") if decision.granted else ToolResult.error("denied")

        def spec(name: str, handler: Any) -> ToolSpec:
            # parallel_safe：工具运行时的读写锁是整个 Engine 共用的，独占的慢工具会把
            # 别的 thread 上的调用一并挡住
            return ToolSpec(
                name=name, description=name,
                input_schema={"type": "object", "properties": {}},
                handler=handler, effect_kind="pure", reconciliation="none",
                parallel_safe=True,
            )

        return [*_spawn_tools(), spec("slow", slow), spec("guarded", guarded)]

    async def start(
        self,
        *,
        root: list[SimTurn] | None = None,
        worker: list[SimTurn] | None = None,
        merge: list[SimTurn] | None = None,
        resume_thread_id: str | None = None,
        extra_tools: list[ToolSpec] | None = None,
    ) -> None:
        self.sim = RoutingSimClient(routes={
            "WORKER-BODY": worker or [],
            "MERGE-BODY": merge or [],
            "ROOT-BODY": root or [],
        })
        self.core = JsonlSessionJournalCore(self.tmp_path / "journal")
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(self.tmp_path),
            threads_dir=self.tmp_path / "threads",
            model_client=AttemptObservableClientAdapter(
                self.sim, provider="sim", default_model="sim-model"
            ),
            compressors=[],
            extra_tools=extra_tools if extra_tools is not None else self._tools(),
            permission_policy=PermissionPolicy(
                rules=[PermissionRule(
                    scope="skill_dispatch", target_pattern="glob:*", mode="allow",
                )],
                default_mode="ask", prompter=SuspendingPrompter(),
            ),
            max_parallel_tool_calls=3,
            audit=AuditConfig(
                journal_core=self.core, writer_id="writer-spawn",
                max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
            ),
        )
        self.engine = await self.pool.get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )

    async def ask(self, text: str = "开始") -> list[Any]:
        return await run_until_root_done(self.engine, taifeng.UserMessage(text=text))

    async def journal(self) -> list[JournalEnvelope]:
        core = JsonlSessionJournalCore(self.tmp_path / "journal")
        return [envelope async for envelope in core.load(_SESSION)]

    async def settled(self, handle_id: str, status: str) -> None:
        await wait_for_condition(
            lambda: self.engine.spawn_status([handle_id])[handle_id]["status"] == status
        )

    def status(self, handle_id: str) -> dict[str, Any]:
        return self.engine.spawn_status([handle_id])[handle_id]


def _of(envelopes: list[JournalEnvelope], record_type: str) -> list[JournalEnvelope]:
    return [e for e in envelopes if e.record_type == record_type]


def _handles(envelopes: list[JournalEnvelope]) -> list[str]:
    return [str(e.payload["handle_id"]) for e in _of(envelopes, "spawn_started")]


# ====================================================================
# 静态门
# ====================================================================


async def test_spawn_tools_are_admitted_and_background_tasks_stay_out(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start()
    await run.pool.close()

    other = _Run(tmp_path / "background")
    with pytest.raises(AuditCapabilityError) as raised:
        await other.start(extra_tools=[
            make_run_in_background_tool(registry=BackgroundTaskRegistry()),
        ])
    assert raised.value.code == "audit_spawn_unsupported"


# ====================================================================
# 发起与结束
# ====================================================================


async def test_spawn_is_journaled_before_the_child_runs(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[
            SimTurn(text="派一个", tool_calls=[
                _call("spawn_skill", "c1", skill_id="worker", args={"n": 1}, reason="并行"),
            ]),
            SimTurn(text="已派出"),
        ],
        worker=[SimTurn(text="活干完了")],
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    (handle_id,) = _handles(await run.journal())
    await run.settled(handle_id, "done")
    assert run.status(handle_id)["result"] == "活干完了"
    envelopes = await run.journal()
    (started,) = _of(envelopes, "spawn_started")
    payload = SpawnStartedV1.model_validate(started.payload)
    root_thread = run.engine.thread_id
    child_thread = child_thread_id_of(_SESSION, handle_id)
    assert (payload.handle_id, payload.skill_id) == (handle_id, "worker")
    assert (payload.parent_thread_id, payload.child_thread_id) == (root_thread, child_thread)
    assert (payload.arguments, payload.reason) == ({"n": 1}, "并行")
    assert started.operation_id == handle_id
    # 发起批次：派发、子 thread 的创建与绑定、种子；都在发起它的工具调用结算之前
    index = envelopes.index(started)
    batch = envelopes[index:index + 4]
    assert [e.record_type for e in batch] == [
        "spawn_started", "thread_created", "thread_bound", "conversation_item",
    ]
    assert [e.thread_id for e in batch] == [root_thread, child_thread, child_thread, child_thread]
    assert batch[3].payload["item_kind"] == "user_message"
    (outcome,) = [
        e for e in _of(envelopes, "tool_outcome_committed") if e.payload["call_id"] == "c1"
    ]
    assert batch[3].seq < outcome.seq
    assert json.loads(outcome.payload["output"]) == {
        "handle_id": handle_id, "child_thread_id": child_thread,
    }
    # 子 skill 的 LLM 调用记在子 thread 名下，且在发起批次之后
    child_requests = [
        e for e in _of(envelopes, "llm_request_committed") if e.thread_id == child_thread
    ]
    assert len(child_requests) == 1
    assert child_requests[0].seq > batch[3].seq
    assert str(child_requests[0].operation_id).startswith(
        f"{child_thread}:{child_thread}:turn:0:llm:"
    )
    # 终态
    (settled,) = _of(envelopes, "spawn_settled")
    end = SpawnSettledV1.model_validate(settled.payload)
    assert (end.status, end.end_reason, end.result) == ("done", "completed", "活干完了")
    assert end.started_record_id == started.record_id
    terminal = envelopes[envelopes.index(settled) + 1]
    assert (terminal.record_type, terminal.thread_id) == ("thread_terminal", child_thread)
    assert terminal.payload["status"] == "done"
    # 任何 thread 上都没有锚点条目
    kinds = {e.payload["item_kind"] for e in _of(envelopes, "conversation_item")}
    assert kinds.isdisjoint({"spawn", "spawn_settled"})
    assert all(item.kind != "spawn" for item in run.engine.history_snapshot())
    # 子 thread 的投影：种子与回复
    child_items = [
        i async for i in await run.pool.store.load_thread(child_thread)
    ]
    assert [i.kind for i in child_items] == ["user_message", "assistant_message"]
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_result_is_read_back_through_the_query_tools(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[SimTurn(text="结论甲")])

    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="直接发起")

    await run.settled(out["handle_id"], "done")
    run.sim._routes["ROOT-BODY"] = [  # noqa: SLF001
        SimTurn(text="查一下", tool_calls=[
            _call("join_skill", "j1", handle_ids=[out["handle_id"]]),
            _call("wait_peer", "w1", handle_id=out["handle_id"], timeout_seconds=5),
        ]),
        SimTurn(text="拿到了"),
    ]
    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    outputs = {
        e.payload["call_id"]: e.payload["output"]
        for e in _of(await run.journal(), "tool_outcome_committed")
    }
    assert json.loads(outputs["j1"])[out["handle_id"]] == {"status": "done", "result": "结论甲"}
    assert "结论甲" in outputs["w1"]
    await run.pool.close()


async def test_two_spawns_run_side_by_side(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[
            SimTurn(text="派两个", tool_calls=[
                _call("spawn_skill", "c1", skill_id="worker", args={"n": 1}, reason="甲"),
                _call("spawn_skill", "c2", skill_id="worker", args={"n": 2}, reason="乙"),
            ]),
            SimTurn(text="都派出了"),
        ],
        worker=[SimTurn(text="结果一"), SimTurn(text="结果二")],
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    handles = _handles(await run.journal())
    assert len(set(handles)) == 2
    for handle_id in handles:
        await run.settled(handle_id, "done")
    envelopes = await run.journal()
    assert {e.payload["handle_id"] for e in _of(envelopes, "spawn_settled")} == set(handles)
    threads = {e.thread_id for e in _of(envelopes, "thread_terminal")}
    assert threads == {child_thread_id_of(_SESSION, h) for h in handles}
    assert find_unsettled_effects(envelopes) == ()
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_kill_settles_the_spawn_as_cancelled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[
        SimTurn(text="先干慢活", tool_calls=[_call("slow", "s1")]),
    ])
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="会被终止")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)

    await run.engine.kill_spawn(out["handle_id"])

    await run.settled(out["handle_id"], "cancelled")
    envelopes = await run.journal()
    (settled,) = _of(envelopes, "spawn_settled")
    assert (settled.payload["status"], settled.payload["end_reason"]) == ("cancelled", "cancelled")
    # 被取消的工具调用也有结果
    assert find_unsettled_effects(envelopes) == ()
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_unjournalable_arguments_are_rejected_before_anything_is_written(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[SimTurn(text="不会运行")])
    before = len(await run.journal())

    with pytest.raises(SpawnRejectedError) as raised:
        await run.engine.spawn_skill(
            skill_id="worker", args={"when": object()}, reason="参数进不了 Journal",
        )

    assert raised.value.reject_reason == "arguments_not_canonical"
    assert len(await run.journal()) == before
    assert not run.engine.has_live_spawns()
    # 名额没有被占走
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="正常")
    await run.settled(out["handle_id"], "done")
    await run.pool.close()


async def test_a_child_that_stops_to_ask_freezes_the_session(tmp_path: Path) -> None:
    """子 thread 上的 turn 不能停下等人：``Resume`` 只认 root thread。"""
    run = _Run(tmp_path)
    await run.start(worker=[
        SimTurn(text="申请写入", tool_calls=[_call("guarded", "g1", key="k")]),
    ])

    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="会去申请审批")

    await run.settled(out["handle_id"], "error")
    envelopes = await run.journal()
    assert _of(envelopes, "turn_suspended") == []
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "error"
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()


async def test_a_synchronous_child_that_stops_to_ask_leaves_no_suspension(
    tmp_path: Path,
) -> None:
    """同步派发的子 skill 同理：冻结发生在那次调用结算时，子 thread 不留挂起记录。"""
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="同步派", tool_calls=[
            _call("call_skill", "k1", skill_id="worker", args={}, reason="同步"),
        ])],
        worker=[SimTurn(text="申请写入", tool_calls=[_call("guarded", "g1", key="k")])],
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_failed", events[-1].msg.data
    assert events[-1].msg.data["kind"] == "SessionAuditFrozenError"
    envelopes = await run.journal()
    assert _of(envelopes, "turn_suspended") == []
    statuses = {
        e.payload["call_id"]: e.payload["status"]
        for e in _of(envelopes, "tool_outcome_committed")
    }
    assert statuses["g1"] == "error"
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()


def test_waits_are_bounded_by_the_settlement_deadline(tmp_path: Path) -> None:
    async def scenario() -> None:
        run = _Run(tmp_path)
        await run.start()
        limit = run.engine._audit_state.coordinator.finalization_timeout  # noqa: SLF001
        assert run.engine._spawn._bounded_wait(limit * 4) == limit / 2  # noqa: SLF001
        assert run.engine._spawn._bounded_wait(1.0) == 1.0  # noqa: SLF001
        await run.pool.close()

    asyncio.run(scenario())


# ====================================================================
# 释放与接管
# ====================================================================


async def test_release_cancels_running_children_and_ends_the_session(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[
        SimTurn(text="先干慢活", tool_calls=[_call("slow", "s1")]),
    ])
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="释放时还在跑")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)

    await run.pool.close()

    envelopes = await run.journal()
    (settled,) = _of(envelopes, "spawn_settled")
    assert settled.payload["handle_id"] == out["handle_id"]
    assert settled.payload["status"] == "cancelled"
    assert len(_of(envelopes, "session_ended")) == 1
    # 子 thread 的终态只有一条
    child = child_thread_id_of(_SESSION, out["handle_id"])
    assert [e.thread_id for e in _of(envelopes, "thread_terminal")].count(child) == 1
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_handles_are_rebuilt_from_the_journal_on_takeover(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")], worker=[SimTurn(text="早先的结论")],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="接管前完成")
    await run.settled(out["handle_id"], "done")
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start(resume_thread_id=thread_id)

    await resumed.settled(out["handle_id"], "done")
    assert resumed.status(out["handle_id"])["result"] == "早先的结论"
    assert not resumed.engine.has_live_spawns()
    await resumed.pool.close()


async def test_a_spawn_interrupted_by_a_crash_is_settled_on_takeover(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")],
        worker=[SimTurn(text="先干慢活", tool_calls=[_call("slow", "s1")])],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="崩溃时还在跑")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    thread_id = run.engine.thread_id
    child = child_thread_id_of(_SESSION, out["handle_id"])
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()
    crashed = await run.journal()
    assert _of(crashed, "spawn_settled") == []
    (spawn,) = spawn_handles_from_journal(crashed)
    assert (spawn.handle_id, spawn.status) == (out["handle_id"], None)

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着来")], resume_thread_id=thread_id)

    assert resumed.status(out["handle_id"]) == {"status": "cancelled", "result": None}
    envelopes = await resumed.journal()
    (settled,) = _of(envelopes, "spawn_settled")
    end = SpawnSettledV1.model_validate(settled.payload)
    assert (end.status, end.end_reason) == ("cancelled", "process_recovery")
    # 子 thread 上悬空的工具调用先结算，再落这次派发的终态
    (recovery,) = [
        e for e in _of(envelopes, "tool_recovery_committed") if e.thread_id == child
    ]
    assert recovery.seq < settled.seq
    assert find_unsettled_effects(envelopes) == ()
    events = await resumed.ask("继续")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


# ====================================================================
# join-barrier
# ====================================================================


async def _journal_has(run: _Run, record_type: str, count: int = 1) -> list[JournalEnvelope]:
    found: list[JournalEnvelope] = []

    async def ready() -> bool:
        found[:] = _of(await run.journal(), record_type)
        return len(found) >= count

    for _ in range(600):
        if await ready():
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"{record_type} x{count} 没有出现")


async def test_barrier_fires_after_every_member_has_settled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        worker=[
            SimTurn(text="甲的结论"),
            SimTurn(tool_calls=[_call("slow", "s1")]),
            SimTurn(text="乙的结论"),
        ],
        merge=[SimTurn(text="汇总完毕")],
    )
    first = await run.engine.spawn_skill(skill_id="worker", args={"n": 1}, reason="甲")
    await run.settled(first["handle_id"], "done")
    second = await run.engine.spawn_skill(skill_id="worker", args={"n": 2}, reason="乙")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    members = [first["handle_id"], second["handle_id"]]

    out = await run.engine.set_join_barrier(members, "merge")

    assert _of(await run.journal(), "barrier_fired") == []
    run.slow_release.set()
    (settled,) = await _journal_has(run, "barrier_settled")
    envelopes = await run.journal()
    barrier_id = out["barrier_id"]
    then_thread = barrier_thread_id_of(_SESSION, barrier_id)
    (registered,) = _of(envelopes, "barrier_registered")
    assert registered.payload["handle_ids"] == members
    assert registered.operation_id == barrier_id
    (fired,) = _of(envelopes, "barrier_fired")
    payload = BarrierFiredV1.model_validate(fired.payload)
    assert payload.registered_record_id == registered.record_id
    assert payload.then_thread_id == then_thread
    assert [(m.handle_id, m.status) for m in payload.members] == [
        (members[0], "done"), (members[1], "done"),
    ]
    assert payload.arguments == {
        members[0]: {"status": "done", "result": "甲的结论"},
        members[1]: {"status": "done", "result": "乙的结论"},
    }
    # 点火在成员全部结束之后；点火批次带着聚合 thread 的创建与种子
    assert max(e.seq for e in _of(envelopes, "spawn_settled")) < fired.seq
    index = envelopes.index(fired)
    batch = envelopes[index:index + 4]
    assert [e.record_type for e in batch] == [
        "barrier_fired", "thread_created", "thread_bound", "conversation_item",
    ]
    assert [e.thread_id for e in batch[1:]] == [then_thread] * 3
    requests = [e for e in _of(envelopes, "llm_request_committed") if e.thread_id == then_thread]
    assert len(requests) == 1
    assert requests[0].seq > batch[3].seq
    end = BarrierSettledV1.model_validate(settled.payload)
    assert (end.status, end.end_reason, end.result) == ("done", "completed", "汇总完毕")
    assert end.fired_record_id == fired.record_id
    terminal = envelopes[envelopes.index(settled) + 1]
    assert (terminal.record_type, terminal.thread_id) == ("thread_terminal", then_thread)
    kinds = {e.payload["item_kind"] for e in _of(envelopes, "conversation_item")}
    assert kinds.isdisjoint({"join_barrier", "join_barrier_fired"})
    assert find_unsettled_effects(envelopes) == ()
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_barrier_registered_through_the_tool_fires_at_once_when_members_are_done(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[SimTurn(text="结论")], merge=[SimTurn(text="汇总")])
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="先跑完")
    await run.settled(out["handle_id"], "done")
    run.sim._routes["ROOT-BODY"] = [  # noqa: SLF001
        SimTurn(text="登记", tool_calls=[_call(
            "await_skills", "a1", handle_ids=[out["handle_id"]], then_skill_id="merge",
            then_args_template={"note": "自定义输入"},
        )]),
        SimTurn(text="已登记"),
    ]

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    await _journal_has(run, "barrier_settled")
    envelopes = await run.journal()
    (fired,) = _of(envelopes, "barrier_fired")
    assert fired.payload["arguments"] == {"note": "自定义输入"}
    (outcome,) = [
        e for e in _of(envelopes, "tool_outcome_committed") if e.payload["call_id"] == "a1"
    ]
    assert json.loads(outcome.payload["output"]) == {"barrier_id": fired.payload["barrier_id"]}
    # 登记先于发起它的工具调用结算
    assert _of(envelopes, "barrier_registered")[0].seq < outcome.seq
    await run.pool.close()


async def test_unjournalable_barrier_input_is_rejected(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(worker=[SimTurn(text="结论")])
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="成员")
    await run.settled(out["handle_id"], "done")

    with pytest.raises(ValueError, match="then_args_not_canonical"):
        await run.engine.set_join_barrier([out["handle_id"]], "merge", {"x": object()})

    envelopes = await run.journal()
    assert _of(envelopes, "barrier_registered") == []
    assert _of(envelopes, "barrier_fired") == []
    await run.pool.close()


async def test_release_cancels_a_running_aggregation(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        worker=[SimTurn(text="结论")],
        merge=[SimTurn(text="先干慢活", tool_calls=[_call("slow", "m1")])],
    )
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="成员")
    await run.settled(out["handle_id"], "done")
    await run.engine.set_join_barrier([out["handle_id"]], "merge")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)

    await run.pool.close()

    envelopes = await run.journal()
    (settled,) = _of(envelopes, "barrier_settled")
    assert settled.payload["status"] == "cancelled"
    assert len(_of(envelopes, "session_ended")) == 1
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def _crash(run: _Run) -> str:
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()
    return thread_id


async def test_a_pending_barrier_fires_after_takeover(tmp_path: Path) -> None:
    """登记了、没点火，成员在崩溃时还在跑：接管把成员落终态，barrier 随即点火。"""
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")],
        worker=[SimTurn(text="先干慢活", tool_calls=[_call("slow", "s1")])],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="崩溃时还在跑")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    registered = await run.engine.set_join_barrier([out["handle_id"]], "merge")
    thread_id = await _crash(run)
    (barrier,) = barriers_from_journal(await run.journal())
    assert (barrier.barrier_id, barrier.fired) == (registered["barrier_id"], False)

    resumed = _Run(tmp_path)
    await resumed.start(merge=[SimTurn(text="成员没跑完")], resume_thread_id=thread_id)

    (settled,) = await _journal_has(resumed, "barrier_settled")
    envelopes = await resumed.journal()
    (fired,) = _of(envelopes, "barrier_fired")
    assert fired.payload["members"] == [
        {"handle_id": out["handle_id"], "status": "cancelled", "payload_version": 1},
    ]
    assert settled.payload["result"] == "成员没跑完"
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_an_interrupted_aggregation_is_settled_and_not_fired_again(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="你好")],
        worker=[SimTurn(text="结论")],
        merge=[SimTurn(text="先干慢活", tool_calls=[_call("slow", "m1")])],
    )
    await run.ask()
    out = await run.engine.spawn_skill(skill_id="worker", args={}, reason="成员")
    await run.settled(out["handle_id"], "done")
    await run.engine.set_join_barrier([out["handle_id"]], "merge")
    await asyncio.wait_for(run.slow_entered.wait(), timeout=10)
    thread_id = await _crash(run)

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着来")], resume_thread_id=thread_id)
    events = await resumed.ask("继续")

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    envelopes = await resumed.journal()
    (settled,) = _of(envelopes, "barrier_settled")
    end = BarrierSettledV1.model_validate(settled.payload)
    assert (end.status, end.end_reason) == ("cancelled", "process_recovery")
    assert len(_of(envelopes, "barrier_fired")) == 1
    assert not resumed.engine.has_live_spawns()
    assert find_unsettled_effects(envelopes) == ()
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
