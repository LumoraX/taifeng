"""strict audit Session resume 端到端：崩溃 → 新 pool 从 Journal 接管续跑（ADR 0053）。

「崩溃」的模拟：``core.close()`` 释放写者锁但不写 ``session_ended``（等价于进程死亡时
内核释放 flock）；随后旧 pool 的收尾因已无 live writer 而无法落 terminal，Journal 因此
停在「无 session_ended」状态。新 pool 用**另一个 core 实例**（模拟另一进程）指向同一
Journal root 与 threads 目录 resume。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import taifeng
import taifeng.loop.pool as pool_module
from taifeng.conversation.journal import (
    ActorRef,
    JournalHealth,
    JournalIdentities,
    JournalRecordFactory,
    ToolIntentCommittedV1,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditEngineCreationError, AuditSessionReleaseError
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.audit_resume import AuditResumeError
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path

_SESSION = "ses-resume"


def _pool_kwargs(
    tmp_path: Path, skills_dir: Path, core: JsonlSessionJournalCore, turns: list[SimTurn]
) -> dict[str, object]:
    """同一 threads 目录 + 独立 core 实例的 audited pool 构造参数。"""
    return {
        "skills_dir": skills_dir,
        "threads_dir": tmp_path / "threads",
        "model_client": AttemptObservableClientAdapter(
            SimClient(turns=turns), provider="sim", default_model="sim-model"
        ),
        "compressors": [],
        "audit": AuditConfig(
            journal_core=core,
            writer_id="writer-resume",
            max_attachment_bytes=65536,
            max_total_attachment_bytes=1048576,
        ),
    }


async def _crashed_pool_session(
    tmp_path: Path, skills_dir: Path
) -> tuple[str, list[taifeng.ResponseItem]]:
    """跑一个 call_skill turn 后模拟崩溃；返回 root thread id 与其投影 history。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    turns = [
        SimTurn(
            text="派发",
            tool_calls=[{
                "id": "c1",
                "name": "call_skill",
                "arguments": '{"skill_id": "style-checker", "reason": "审查"}',
            }],
        ),
        SimTurn(text="风格审查完成"),
        SimTurn(text="第一轮结论"),
    ]
    pool = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, turns))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="code-reviewer")
    op = taifeng.UserMessage(text="第一轮")
    assert await run_until_root_done_kind(engine, op) == "turn_completed"
    thread_id = engine.thread_id
    # 崩溃：Journal writer 消失（锁随之释放），旧 pool 收尾无法再落 session_ended
    await core.close()
    with pytest.raises(AuditSessionReleaseError) as released:
        await pool.close()
    assert released.value.finish_result.audit_complete is False
    history = [item async for item in await pool._store.load_thread(thread_id)]  # noqa: SLF001
    return thread_id, history


async def _types(root: Path) -> list[tuple[int, str]]:
    """读取 (writer_epoch, record_type) 序列。"""
    core = JsonlSessionJournalCore(root)
    return [(e.writer_epoch, e.record_type) async for e in core.load(_SESSION)]


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_after_crash_continues_turn(
    tmp_path: Path, skills_dir: Path
) -> None:
    """崩溃后新 pool resume：history 与投影一致、续跑新 turn、verify 通过且含 epoch 2。"""
    thread_id, projected = await _crashed_pool_session(tmp_path, skills_dir)
    before = await _types(tmp_path / "journal")
    assert "session_ended" not in {t for _, t in before}

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(
        **_pool_kwargs(tmp_path, skills_dir, core, [SimTurn(text="第二轮结论")])
    )
    engine = await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )

    assert engine.thread_id == thread_id
    assert [item.id for item in engine.history_snapshot()] == [item.id for item in projected]
    assert engine.history_snapshot() == projected
    op = taifeng.UserMessage(text="第二轮")
    assert await run_until_root_done_kind(engine, op) == "turn_completed"
    await pool.close()

    verification = await core.verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    after = await _types(tmp_path / "journal")
    assert after[: len(before)] == before
    resumed = after[len(before):]
    assert resumed[0] == (2, "writer_takeover")
    assert all(epoch == 2 for epoch, _ in resumed)
    assert resumed[-1] == (2, "session_ended")
    envelopes = [e async for e in core.load(_SESSION)]
    accepted = [e for e in envelopes if e.record_type == "submission_accepted"]
    assert [e.payload["turn_index"] for e in accepted] == [0, 1]
    # 投影 thread 被复用并续写：第二轮 user/assistant 追加在第一轮之后
    store_items = [item async for item in await pool._store.load_thread(thread_id)]  # noqa: SLF001
    assert store_items[: len(projected)] == projected
    assert [item.kind for item in store_items[len(projected):]] == [
        "user_message",
        "assistant_message",
    ]


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_after_clean_shutdown_raises_session_ended(
    tmp_path: Path, skills_dir: Path
) -> None:
    """正常关停（已写 session_ended）后 resume 显式拒绝，且不写接管记录。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(
        **_pool_kwargs(tmp_path, skills_dir, core, [SimTurn(text="好")])
    )
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="code-reviewer")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text="hi")) == (
        "turn_completed"
    )
    thread_id = engine.thread_id
    await pool.close()
    before = await _types(tmp_path / "journal")

    fresh = JsonlSessionJournalCore(tmp_path / "journal")
    resumed = await taifeng.EnginePool.create(
        **_pool_kwargs(tmp_path, skills_dir, fresh, [])
    )
    with pytest.raises(AuditResumeError) as caught:
        await resumed.get_or_create(
            session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=thread_id
        )

    assert caught.value.code == "audit_resume_session_ended"
    assert await _types(tmp_path / "journal") == before
    await resumed.close()


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_with_unsettled_tool_intent_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """存在无 outcome 的 tool intent（结果未知）：resume 拒绝并列出该 record，不写接管。"""
    thread_id, _ = await _crashed_pool_session(tmp_path, skills_dir)
    # 另一个 writer 接管后写下 intent 随即崩溃：effect 是否发生无从得知
    writer = JsonlSessionJournalCore(tmp_path / "journal")
    opened = await writer.open_existing(_SESSION, writer_id="w-mid", operation_id="mid")
    identities = JournalIdentities(
        session_id=_SESSION, thread_id=thread_id, submission_id="sub-lost"
    )
    factory = JournalRecordFactory(
        session_id=_SESSION, actor=ActorRef(kind="system", source="test"),
        identities=identities,
    )
    turn = identities.turn(9)
    intent = factory.build(
        operation_id=identities.tool(turn, "call-lost"),
        record_type="tool_intent_committed",
        payload=ToolIntentCommittedV1(
            turn_index=9, iteration=1, call_id="call-lost", name="external_write",
            arguments_raw="{}", effective_arguments={}, parallel_safe=False,
            effect_kind="external_non_idempotent", reconciliation="manual",
        ),
        thread_id=thread_id,
        turn_id=turn,
    )
    await writer.append(intent, lease=opened.lease, expected_seq=opened.ack.last_seq)
    await writer.close()
    before = await _types(tmp_path / "journal")

    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, []))
    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=thread_id
        )

    assert caught.value.code == "audit_resume_recovery_required"
    assert caught.value.record_ids == (intent.record_id,)
    assert await _types(tmp_path / "journal") == before
    assert pool._engines == {}  # noqa: SLF001
    # 拒绝后不残留写者锁：另一实例仍可接管
    probe = JsonlSessionJournalCore(tmp_path / "journal")
    await probe.open_existing(_SESSION, writer_id="probe", operation_id="probe")
    await probe.close()
    await pool.close()


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_while_writer_live_raises_busy(
    tmp_path: Path, skills_dir: Path
) -> None:
    """原 pool 仍存活（持锁）时另一 pool resume 同一 Session → busy，不产生写入。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    live = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, []))
    engine = await live.get_or_create(session_id=_SESSION, entry_skill_id="code-reviewer")
    other_core = JsonlSessionJournalCore(tmp_path / "journal")
    other = await taifeng.EnginePool.create(
        **_pool_kwargs(tmp_path, skills_dir, other_core, [])
    )

    with pytest.raises(AuditResumeError) as caught:
        await other.get_or_create(
            session_id=_SESSION,
            entry_skill_id="code-reviewer",
            resume_thread_id=engine.thread_id,
        )

    assert caught.value.code == "audit_resume_busy"
    await other.close()
    await live.close()


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_unknown_thread_raises_marker_missing(
    tmp_path: Path, skills_dir: Path
) -> None:
    """不存在 / 非 audited 的 thread 不得被 audit resume 接受。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, []))

    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id="thr_nope"
        )

    assert caught.value.code == "audit_resume_marker_missing"
    await pool.close()


@pytest.mark.asyncio
async def test_get_or_create_audited_cache_hit_other_thread_raises_session_active(
    tmp_path: Path, skills_dir: Path
) -> None:
    """audited Session 已 live：同 thread 的 resume 命中缓存，其他 thread 被拒。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, []))
    engine = await pool.get_or_create(session_id=_SESSION, entry_skill_id="code-reviewer")

    same = await pool.get_or_create(
        session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=engine.thread_id
    )
    with pytest.raises(AuditResumeError) as caught:
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id="thr_other"
        )

    assert same is engine
    assert caught.value.code == "audit_resume_session_active"
    await pool.close()


@pytest.mark.asyncio
async def test_get_or_create_audited_resume_engine_failure_releases_lease_without_ending(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume 后 Engine 构造失败：只释放 lease、不写 session_ended，之后可再次接管。"""
    thread_id, _ = await _crashed_pool_session(tmp_path, skills_dir)
    core = JsonlSessionJournalCore(tmp_path / "journal")
    pool = await taifeng.EnginePool.create(**_pool_kwargs(tmp_path, skills_dir, core, []))

    def _broken_engine(**kwargs: object) -> taifeng.AgentEngine:
        raise RuntimeError("engine construction failed")

    monkeypatch.setattr(pool_module, "AgentEngine", _broken_engine)
    with pytest.raises(AuditEngineCreationError):
        await pool.get_or_create(
            session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=thread_id
        )
    monkeypatch.undo()

    types = await _types(tmp_path / "journal")
    assert types[-1] == (2, "writer_takeover")
    assert "session_ended" not in {t for _, t in types}
    retry = await taifeng.EnginePool.create(
        **_pool_kwargs(tmp_path, skills_dir, JsonlSessionJournalCore(tmp_path / "journal"), [])
    )
    engine = await retry.get_or_create(
        session_id=_SESSION, entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )
    assert engine.thread_id == thread_id
    await retry.close()
    final = await _types(tmp_path / "journal")
    assert final[len(types)] == (3, "writer_takeover")
    assert final[-1] == (3, "session_ended")
    await pool.close()
