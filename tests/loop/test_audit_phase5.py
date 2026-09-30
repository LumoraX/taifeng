"""Journal Phase 5（ADR 0104）：Timeline 投影、脱敏视图、旧 transcript 导入、投影重建。"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.legacy_import import (
    LEGACY_SOURCE_LINE_KEY,
    LegacyImportError,
    LegacyImportV1,
    import_legacy_transcript,
)
from taifeng.conversation.journal.projection_rebuild import rebuild_projections
from taifeng.conversation.journal.redaction import redact_payload
from taifeng.conversation.journal.timeline import JournalTimelineProjector, TimelineFilter
from taifeng.conversation.models import spawn_item, user_message
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_resume import AuditResumeError
from tests.loop.test_audit_spawn import _SESSION, _call, _of, _Run

if TYPE_CHECKING:
    from pathlib import Path


async def _session_with_a_tool_call(tmp_path: Path) -> tuple[_Run, str]:
    run = _Run(tmp_path)
    await run.start(root=[
        SimTurn(text="先干活", tool_calls=[_call("slow", "r1", key="秘密参数")]),
        SimTurn(text="做完了"),
    ])
    run.slow_release.set()
    events = await run.ask("请处理这份材料")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    return run, run.engine.thread_id


# ====================================================================
# Timeline
# ====================================================================


async def test_timeline_maps_every_record_in_order_with_backlinks(tmp_path: Path) -> None:
    run, thread_id = await _session_with_a_tool_call(tmp_path)
    await run.pool.close()
    core = JsonlSessionJournalCore(tmp_path / "journal")
    envelopes = [e async for e in core.load(_SESSION)]

    page = await JournalTimelineProjector(core).read(_SESSION)

    assert [i.seq for i in page.items] == [e.seq for e in envelopes]
    assert [i.record_id for i in page.items] == [e.record_id for e in envelopes]
    assert page.last_seq == envelopes[-1].seq
    assert page.audit_complete is True
    first = page.items[0]
    assert (first.record_type, first.actor_kind) == ("session_started", "system")
    assert first.recorded_at == envelopes[0].recorded_at
    user = next(i for i in page.items if i.record_type == "submission_accepted")
    assert user.payload["text"] == "请处理这份材料"
    assert user.actor_kind == "user"
    intent = next(i for i in page.items if i.record_type == "tool_intent_committed")
    assert (intent.call_id, intent.thread_id) == ("r1", thread_id)
    assert intent.turn_id is not None and intent.submission_id is not None
    # 用户输入能从 Timeline 完整读取；模型输出与工具调用参数照样在
    assert any(i.record_type == "conversation_item" and i.payload["payload"].get("text") == "做完了"
               for i in page.items)


async def test_timeline_filters_and_resumes_from_a_cursor(tmp_path: Path) -> None:
    run, thread_id = await _session_with_a_tool_call(tmp_path)
    await run.pool.close()
    core = JsonlSessionJournalCore(tmp_path / "journal")
    projector = JournalTimelineProjector(core)
    full = await projector.read(_SESSION)

    by_call = await projector.read(_SESSION, filter=TimelineFilter(call_id="r1"))
    assert {i.record_type for i in by_call.items} >= {
        "tool_intent_committed", "tool_outcome_committed",
    }
    assert all(i.call_id == "r1" for i in by_call.items)
    by_type = await projector.read(
        _SESSION, filter=TimelineFilter(record_types={"llm_request_committed"}),
    )
    assert [i.record_type for i in by_type.items] == ["llm_request_committed"] * 2
    by_actor = await projector.read(_SESSION, filter=TimelineFilter(actor_kind="user"))
    assert all(i.actor_kind == "user" for i in by_actor.items) and by_actor.items
    turn = full.items[-3].turn_id
    by_turn = await projector.read(_SESSION, filter=TimelineFilter(turn_id=turn))
    assert by_turn.items and all(i.turn_id == turn for i in by_turn.items)
    # 分页：limit 与 after_seq 接力，不漏不重
    seen: list[int] = []
    cursor = 0
    while True:
        page = await projector.read(_SESSION, after_seq=cursor, limit=4)
        seen.extend(i.seq for i in page.items)
        if len(page.items) < 4:
            break
        cursor = page.last_seq
    assert seen == [i.seq for i in full.items]
    with pytest.raises(ValueError, match="after_seq"):
        await projector.read(_SESSION, after_seq=-1)


# ====================================================================
# 脱敏
# ====================================================================


def test_redaction_is_deterministic_and_keeps_structure() -> None:
    payload = {
        "call_id": "c1", "name": "guarded", "status": "success",
        "output": "内容 A", "effective_arguments": {"key": "秘密"},
        "attachments": [{"kind": "image", "media_type": "image/png", "content": "QUJD"}],
    }

    first = redact_payload(payload)
    second = redact_payload(payload)

    assert first == second
    assert first.payload["call_id"] == "c1" and first.payload["status"] == "success"
    assert first.payload["output"]["redacted"] is True
    assert first.payload["output"]["length"] == len("内容 A")
    assert first.payload["effective_arguments"]["redacted"] is True
    assert first.payload["attachments"][0]["kind"] == "image"
    assert first.payload["attachments"][0]["content"]["redacted"] is True
    assert [e.path for e in first.manifest] == [
        "output", "effective_arguments", "attachments[0].content",
    ]
    assert first.original_payload_hash == redact_payload(payload).original_payload_hash
    assert "内容 A" not in json.dumps(first.payload, ensure_ascii=False)


async def test_timeline_views_full_redacted_and_metadata_only(tmp_path: Path) -> None:
    run, _ = await _session_with_a_tool_call(tmp_path)
    await run.pool.close()
    core = JsonlSessionJournalCore(tmp_path / "journal")
    projector = JournalTimelineProjector(core)

    full = await projector.read(_SESSION)
    redacted = await projector.read(_SESSION, view="redacted")
    metadata = await projector.read(_SESSION, view="metadata_only")

    text = json.dumps([i.payload for i in redacted.items], ensure_ascii=False)
    assert "请处理这份材料" not in text and "秘密参数" not in text and "做完了" not in text
    assert redacted.audit_complete is True
    for original, shown in zip(full.items, redacted.items, strict=True):
        assert shown.payload_hash == original.payload_hash
        assert shown.record_id == original.record_id
    assert any(i.redactions for i in redacted.items)
    assert metadata.audit_complete is False
    assert all(i.payload == {} and i.payload_hash for i in metadata.items)
    assert [i.record_type for i in metadata.items] == [i.record_type for i in full.items]


# ====================================================================
# 旧 transcript 导入
# ====================================================================


async def _legacy_thread(tmp_path: Path) -> str:
    """跑一个非审计 Session，留下一份旧 transcript。"""
    from tests.loop.test_audit_spawn import _skills

    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path),
        threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[SimTurn(text="旧时的回答")]),
        compressors=[],
    )
    engine = await pool.get_or_create(session_id="legacy", entry_skill_id="entry")
    from tests.conftest import run_until_root_done

    done = await run_until_root_done(engine, taifeng.UserMessage(text="旧时的问题"))
    assert done[-1].msg.kind == "turn_completed"
    thread_id = engine.thread_id
    # 一条 Journal 不认识的运行态锚点：导入时跳过并记录
    await pool.store.append(spawn_item(
        handle_id="sp_old", skill_id="worker", child_thread_id="thr_old", thread_id=thread_id,
    ))
    await pool.close()
    return thread_id


async def test_a_legacy_transcript_is_imported_and_can_be_taken_over(tmp_path: Path) -> None:
    thread_id = await _legacy_thread(tmp_path)
    store = JsonlMessageStore(tmp_path / "threads")
    core = JsonlSessionJournalCore(tmp_path / "journal")
    original = (tmp_path / "threads" / f"{thread_id}.jsonl").read_bytes()

    result = await import_legacy_transcript(
        journal_core=core, store=store, session_id=_SESSION, thread_id=thread_id,
        writer_id="importer", max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
    )

    assert (result.imported_count, result.skipped) == (2, ((4, "spawn"),))
    assert result.archived_to.read_bytes() == original
    envelopes = [e async for e in core.load(_SESSION)]
    (lead,) = _of(envelopes, "legacy_import")
    payload = LegacyImportV1.model_validate(lead.payload)
    assert payload.history_status == "legacy_unverified"
    assert payload.line_count == 4 and payload.imported_count == 2
    items = _of(envelopes, "conversation_item")
    assert [i.payload["payload"]["text"] for i in items] == ["旧时的问题", "旧时的回答"]
    assert [i.payload["metadata"][LEGACY_SOURCE_LINE_KEY] for i in items] == [2, 3]
    assert all(i.payload["source_record_id"] == lead.record_id for i in items)
    assert envelopes[1].thread_id == thread_id
    verification = await core.verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY
    # 投影已带 marker，内容与 Journal 一致；接管后接着聊
    assert (await store.audited_projection_marker(thread_id)) is not None
    projected = [i async for i in await store.load_thread(thread_id)]
    assert [i.payload["text"] for i in projected] == ["旧时的问题", "旧时的回答"]
    await store.close()

    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="接着旧的说")], resume_thread_id=thread_id)
    assert [i.payload["text"] for i in resumed.engine.history_snapshot()] == [
        "旧时的问题", "旧时的回答",
    ]
    events = await resumed.ask("新问题")
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    assert "旧时的回答" in resumed.sim.ledger.requests()[-1].blob()
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_legacy_import_refuses_what_it_cannot_vouch_for(tmp_path: Path) -> None:
    thread_id = await _legacy_thread(tmp_path)
    store = JsonlMessageStore(tmp_path / "threads")
    core = JsonlSessionJournalCore(tmp_path / "journal")
    common = {
        "journal_core": core, "store": store, "writer_id": "importer",
        "max_attachment_bytes": 65536, "max_total_attachment_bytes": 1048576,
    }

    with pytest.raises(LegacyImportError) as missing:
        await import_legacy_transcript(session_id="s1", thread_id="thr_nope", **common)  # type: ignore[arg-type]
    assert missing.value.code == "legacy_transcript_missing"
    # 审计 Session 接管前必须导入
    run = _Run(tmp_path)
    with pytest.raises(AuditResumeError) as refused:
        await run.start(resume_thread_id=thread_id)
    assert refused.value.code == "audit_resume_marker_missing"
    # 损坏的行不静默跳过
    path = tmp_path / "threads" / f"{thread_id}.jsonl"
    path.write_bytes(path.read_bytes() + b"{not json\n")
    with pytest.raises(LegacyImportError) as corrupt:
        await import_legacy_transcript(session_id="s2", thread_id=thread_id, **common)  # type: ignore[arg-type]
    assert corrupt.value.code == "legacy_transcript_line_invalid"
    assert [e async for e in core.load("s2")] == []
    await store.close()


# ====================================================================
# 投影重建
# ====================================================================


async def test_projections_are_rebuilt_from_the_journal_after_deletion(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        root=[SimTurn(text="派一个", tool_calls=[
            _call("spawn_skill", "c1", skill_id="worker", args={"n": 1}, reason="并行"),
        ]), SimTurn(text="派出了")],
        worker=[SimTurn(text="活干完了")],
    )
    await run.ask("开始")
    (handle_id,) = [str(e.payload["handle_id"]) for e in _of(await run.journal(), "spawn_started")]
    await run.settled(handle_id, "done")
    thread_id = run.engine.thread_id
    # 进程「死掉」而不是正常终结：终结过的 Session 不能再接管
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()
    threads = tmp_path / "threads"
    before = {p.name: p.read_bytes() for p in threads.glob("*.jsonl")}
    assert len(before) == 2
    shutil.rmtree(threads)

    store = JsonlMessageStore(threads)
    core = JsonlSessionJournalCore(tmp_path / "journal")
    result = await rebuild_projections(journal=core, store=store, session_id=_SESSION)

    assert {t.thread_id for t in result.threads} == {p.removesuffix(".jsonl") for p in before}
    assert all(t.created and t.status == "rebuilt" for t in result.threads)
    assert result.divergent == ()
    for name, content in before.items():
        rebuilt = (threads / name).read_bytes()
        # 首行元数据的时间戳不同，对话项逐字相同
        assert rebuilt.splitlines()[1:] == content.splitlines()[1:]
    # 再跑一次是幂等的
    again = await rebuild_projections(journal=core, store=store, session_id=_SESSION)
    assert all(not t.created and t.status == "rebuilt" for t in again.threads)
    await store.close()
    # 重建后的投影可以接管，历史不变
    resumed = _Run(tmp_path)
    await resumed.start(root=[SimTurn(text="回来了")], resume_thread_id=thread_id)
    assert resumed.status(handle_id) == {"status": "done", "result": "活干完了"}
    kinds = [i.kind for i in resumed.engine.history_snapshot()]
    assert kinds[0] == "user_message" and "function_call_output" in kinds
    await resumed.pool.close()


async def test_a_divergent_projection_is_reported_not_overwritten(tmp_path: Path) -> None:
    run, thread_id = await _session_with_a_tool_call(tmp_path)
    await run.pool.close()
    path = tmp_path / "threads" / f"{thread_id}.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    forged = user_message("篡改", thread_id=thread_id)
    lines[1] = (json.dumps(forged.model_dump(mode="json"), ensure_ascii=False) + "\n").encode()
    path.write_bytes(b"".join(lines))
    store = JsonlMessageStore(tmp_path / "threads")
    core = JsonlSessionJournalCore(tmp_path / "journal")

    result = await rebuild_projections(journal=core, store=store, session_id=_SESSION)

    assert result.divergent == (thread_id,)
    assert path.read_bytes() == b"".join(lines)
    await store.close()
