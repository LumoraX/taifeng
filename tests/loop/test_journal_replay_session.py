"""录后整条重放（ADR 0105）：LLM 与工具都来自录制，按录制的提交序列驱动新 Engine。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.replay import JournalReplayClient
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.replay_session import recorded_submissions, replay_session
from taifeng.permission import PermissionPolicy, PermissionRule
from taifeng.tool.replay import recorded_tool_calls, replay_tools
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import wait_for_condition
from tests.loop.test_audit_spawn import _SESSION, _call, _handles, _Run, _skills, _spawn_tools

if TYPE_CHECKING:
    from pathlib import Path


async def _replay_pool(
    tmp_path: Path, records: list[Any], tools: list[ToolSpec], **overrides: Any,
) -> tuple[taifeng.EnginePool, JournalReplayClient]:
    """重放用的审计 pool：同一个 session id、独立的 Journal 根，派生的 thread id 与录制相同。"""
    client = JournalReplayClient.from_records(records)
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path / "replay"), threads_dir=tmp_path / "replay" / "threads",
        model_client=AttemptObservableClientAdapter(client, provider="sim", default_model="sim-model"),
        compressors=[], extra_tools=tools,
        permission_policy=overrides.pop("permission_policy", PermissionPolicy(default_mode="allow")),
        audit=AuditConfig(
            journal_core=JsonlSessionJournalCore(tmp_path / "replay" / "journal"),
            writer_id="replayer", max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
        ),
        **overrides,
    )
    return pool, client


def _counting_tool(name: str, calls: list[dict[str, Any]], output: str) -> ToolSpec:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append(dict(args))
        return ToolResult.ok(f"{output}:{args.get('key')}")

    return ToolSpec(
        name=name, description=name,
        input_schema={"type": "object", "properties": {}},
        handler=handler, effect_kind="pure", reconciliation="none", parallel_safe=True,
    )


async def _record(tmp_path: Path) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """录一段：root 调工具、派一个 worker，两轮对话。"""
    executed: list[dict[str, Any]] = []
    run = _Run(tmp_path)
    await run.start(
        root=[
            SimTurn(tool_calls=[_call("slow", "r1", key="甲")]),
            SimTurn(text="第一轮结束"),
            SimTurn(tool_calls=[
                _call("spawn_skill", "c1", skill_id="worker", args={"n": 1}, reason="并行"),
                _call("slow", "r2", key="乙"),
            ]),
            SimTurn(text="第二轮结束"),
        ],
        worker=[SimTurn(text="工人的结论")],
        extra_tools=[*_spawn_tools(), _counting_tool("slow", executed, "结果")],
    )
    replies: list[str] = []
    for text in ("第一问", "第二问"):
        events = await run.ask(text)
        assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
        replies.append(events[-1].msg.data.get("final_text") or "")
    for e in await run.journal():
        if e.record_type == "spawn_started":
            await run.settled(str(e.payload["handle_id"]), "done")
    await run.pool.close()
    core = JsonlSessionJournalCore(tmp_path / "journal")
    records = [e async for e in core.load(_SESSION)]
    return records, executed, [t for t in ("第一轮结束", "第二轮结束")]


async def test_a_recorded_session_replays_without_network_or_tools(tmp_path: Path) -> None:
    records, executed, _ = await _record(tmp_path)
    assert [c["key"] for c in executed] == ["甲", "乙"]
    calls = recorded_tool_calls(records)
    assert sorted((c.name, c.status) for c in calls if c.name == "slow") == [
        ("slow", "success"), ("slow", "success"),
    ]
    replayed_executed: list[dict[str, Any]] = []
    tools, ledger = replay_tools(
        [*_spawn_tools(), _counting_tool("slow", replayed_executed, "不会执行")], calls,
    )
    pool, client = await _replay_pool(tmp_path, records, tools)
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="entry")

    report = await replay_session(engine, recorded_submissions(records))

    assert not report.diverged, report
    assert [s.outcome for s in report.steps] == ["turn_completed", "turn_completed"]
    # 派出去的 worker 在后台重放（内核编排工具照常运行），等它跑完
    await wait_for_condition(lambda: not engine.has_live_spawns())
    assert replayed_executed == []
    assert ledger.consumed and ledger.remaining == 0
    assert client.remaining == 0
    assert list(engine.spawn_status([h for h in _handles(records)]).values())[0]["result"] == "工人的结论"
    texts = [i.payload.get("text") for i in engine.history_snapshot() if i.kind == "assistant_message"]
    assert "第一轮结束" in texts and "第二轮结束" in texts
    outputs = [i.payload["output"] for i in engine.history_snapshot() if i.kind == "function_call_output"]
    assert "结果:甲" in outputs and "结果:乙" in outputs
    await pool.close()


async def test_replay_reports_the_step_where_the_run_diverges(tmp_path: Path) -> None:
    records, _, _ = await _record(tmp_path)
    tools, ledger = replay_tools([*_spawn_tools(), _counting_tool("slow", [], "x")], recorded_tool_calls(records))
    pool, _ = await _replay_pool(tmp_path, records, tools)
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="entry")
    submissions = recorded_submissions(records)
    # 用户说了录制里没有的话：第一步就分叉
    changed = [submissions[0].__class__(
        submission_id=submissions[0].submission_id, kind="user_message", text="另一个问题",
    ), *submissions[1:]]

    report = await replay_session(engine, changed)

    assert report.diverged_at == submissions[0].submission_id
    assert report.steps[0].outcome == "ReplayDivergenceError"
    assert len(report.steps) == 1
    await pool.close()


async def test_replay_reproduces_a_suspension_and_its_resolution(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(root=[
        SimTurn(tool_calls=[_call("guarded", "g1", key="k")]),
        SimTurn(text="批准之后做完了"),
    ])
    from taifeng.loop.submission import Resume
    from tests.loop.test_audit_suspension import _drive

    events = await _drive(run.engine, taifeng.UserMessage(text="需要审批的事"))
    assert events[-1].msg.kind == "turn_suspended", events[-1].msg.data
    record = run.engine._find_active_suspension()  # noqa: SLF001
    assert record is not None
    (pending,) = record.pending

    done = await _drive(run.engine, Resume(
        thread_id=run.engine.thread_id, resolutions={pending.request_id: {"granted": True}},
    ))
    assert done[-1].msg.kind == "turn_completed", done[-1].msg.data
    await run.pool.close()
    records = [e async for e in JsonlSessionJournalCore(tmp_path / "journal").load(_SESSION)]
    submissions = recorded_submissions(records)
    assert [s.kind for s in submissions] == ["user_message", "resume"]
    tools, ledger = replay_tools(run._tools(), recorded_tool_calls(records))  # noqa: SLF001
    pool, _ = await _replay_pool(tmp_path, records, tools, permission_policy=PermissionPolicy(
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        default_mode="ask", prompter=run.pool._permission_policy.prompter,  # noqa: SLF001
    ))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="entry")

    report = await replay_session(engine, submissions)

    # 录制里停下等人的调用在重放里也停下（重现同一个待答请求），录制的答复结清它
    assert not report.diverged, report
    assert [s.outcome for s in report.steps] == ["turn_suspended", "turn_completed"]
    assert ledger.remaining == 0
    texts = [i.payload.get("text") for i in engine.history_snapshot() if i.kind == "assistant_message"]
    assert "批准之后做完了" in texts
    await pool.close()


async def _record_across_a_takeover(tmp_path: Path) -> tuple[list[Any], _Run]:
    """录制：停下等审批 → 释放 → 新 Engine 接管 → 答复 → 完成。"""
    from taifeng.loop.submission import Resume
    from tests.loop.test_audit_suspension import _drive

    run = _Run(tmp_path)
    await run.start(root=[SimTurn(tool_calls=[_call("guarded", "g1", key="k")])])
    events = await _drive(run.engine, taifeng.UserMessage(text="需要审批的事"))
    assert events[-1].msg.kind == "turn_suspended", events[-1].msg.data
    thread_id = run.engine.thread_id
    await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="批准之后做完了")], resume_thread_id=thread_id)
    record = resumed.engine._find_active_suspension()  # noqa: SLF001
    assert record is not None
    (pending,) = record.pending
    done = await _drive(resumed.engine, Resume(
        thread_id=thread_id, resolutions={pending.request_id: {"granted": True}},
    ))
    assert done[-1].msg.kind == "turn_completed", done[-1].msg.data
    await resumed.pool.close()
    records = [e async for e in JsonlSessionJournalCore(tmp_path / "journal").load(_SESSION)]
    return records, resumed


def _asking_policy(run: _Run) -> PermissionPolicy:
    return PermissionPolicy(
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        default_mode="ask", prompter=run.pool._permission_policy.prompter,  # noqa: SLF001
    )


async def test_replay_reproduces_a_recorded_takeover(tmp_path: Path) -> None:
    records, run = await _record_across_a_takeover(tmp_path)
    submissions = recorded_submissions(records)
    assert [s.kind for s in submissions] == ["user_message", "takeover", "resume"]
    tools, ledger = replay_tools(run._tools(), recorded_tool_calls(records))  # noqa: SLF001
    client = JournalReplayClient.from_records(records)
    live: dict[str, Any] = {}

    async def open_pool(resume_thread_id: str | None = None) -> taifeng.AgentEngine:
        # 回放客户端与工具台账跨 Engine 沿用：录制的消费进度不随重建而重置
        live["pool"] = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path / "replay"), threads_dir=tmp_path / "replay" / "threads",
            model_client=AttemptObservableClientAdapter(
                client, provider="sim", default_model="sim-model",
            ),
            compressors=[], extra_tools=tools, permission_policy=_asking_policy(run),
            audit=AuditConfig(
                journal_core=JsonlSessionJournalCore(tmp_path / "replay" / "journal"),
                writer_id="replayer", max_attachment_bytes=65536,
                max_total_attachment_bytes=1048576,
            ),
        )
        live["engine"] = await live["pool"].get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )
        return live["engine"]

    async def reopen(current: taifeng.AgentEngine) -> taifeng.AgentEngine:
        thread_id = current.thread_id
        await live["pool"].close()
        return await open_pool(thread_id)

    report = await replay_session(await open_pool(), submissions, reopen=reopen)

    assert not report.diverged, report
    assert [s.outcome for s in report.steps] == ["turn_suspended", "reopened", "turn_completed"]
    assert ledger.remaining == 0 and client.remaining == 0
    await live["pool"].close()
    replayed = [
        e.record_type
        async for e in JsonlSessionJournalCore(tmp_path / "replay" / "journal").load(_SESSION)
    ]
    # 重放的 Journal 里也有同样的释放与接管
    assert "session_detached" in replayed and "writer_takeover" in replayed


async def test_a_takeover_without_reopen_is_reported_as_skipped(tmp_path: Path) -> None:
    records, run = await _record_across_a_takeover(tmp_path)
    tools, _ = replay_tools(run._tools(), recorded_tool_calls(records))  # noqa: SLF001
    pool, _ = await _replay_pool(tmp_path, records, tools, permission_policy=_asking_policy(run))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="entry")

    report = await replay_session(engine, recorded_submissions(records))

    assert [(s.kind, s.outcome) for s in report.steps[:2]] == [
        ("user_message", "turn_suspended"), ("takeover", "takeover_skipped"),
    ]
    # 沿用原 Engine 时缓存断点还在，接管后的第一次请求与录制对不上
    assert report.steps[2].outcome == "ReplayDivergenceError"
    assert report.diverged_at == report.steps[2].recorded_submission_id
    await pool.close()


async def test_a_failed_reopen_stops_the_replay_at_the_takeover(tmp_path: Path) -> None:
    records, run = await _record_across_a_takeover(tmp_path)
    tools, _ = replay_tools(run._tools(), recorded_tool_calls(records))  # noqa: SLF001
    pool, _ = await _replay_pool(tmp_path, records, tools, permission_policy=_asking_policy(run))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="entry")

    async def reopen(current: taifeng.AgentEngine) -> taifeng.AgentEngine:
        raise RuntimeError("cannot take over")

    report = await replay_session(engine, recorded_submissions(records), reopen=reopen)

    takeover = report.steps[-1]
    assert (takeover.kind, takeover.outcome, takeover.error) == (
        "takeover", "RuntimeError", "cannot take over",
    )
    assert report.diverged_at == takeover.recorded_submission_id
    await pool.close()
