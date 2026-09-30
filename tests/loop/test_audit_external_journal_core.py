"""审计模式跑在非默认的 Journal 后端上（ADR 0114）。

一致性检查验证的是 core 自身的行为；这里验证内核对 core 没有协议之外的依赖：同一段审计会话——
工具调用处停下等审批 → 释放（分离）→ 新进程接管 → ``Resume`` → 终结——在两种非默认后端上照样成立。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from taifeng.conversation.journal.backend import SessionJournalCore, verify_envelopes
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.memory import InMemoryJournalStorage, InMemorySessionJournalCore
from taifeng.llm.providers.sim import SimTurn
from taifeng.loop.audit_resume import find_unsettled_effects
from tests.loop.test_audit_suspension import _GUARDED, _SESSION, _Run
from tests.testing.test_journal_conformance import (
    _MemoryFileAdapter,
    _MemoryLockAdapter,
    _SharedBytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _memory_cores(tmp_path: Path) -> Callable[[], Any]:
    """换 core：每次给一个接在同一份内存存储上的新实例。"""
    storage = InMemoryJournalStorage()
    return lambda: InMemorySessionJournalCore(storage)


def _adapted_jsonl_cores(tmp_path: Path) -> Callable[[], Any]:
    """换存储：内核的 JSONL core + 内存里的存储与锁适配器。"""
    shared = _SharedBytes()
    return lambda: JsonlSessionJournalCore(
        tmp_path / "journal",
        sync_file_adapter=_MemoryFileAdapter(shared),
        writer_lock_adapter=_MemoryLockAdapter(shared),
    )


@pytest.mark.parametrize("backend", [_memory_cores, _adapted_jsonl_cores])
async def test_an_audited_session_survives_release_and_takeover_on_another_backend(
    tmp_path: Path, backend: Callable[[Path], Callable[[], Any]],
) -> None:
    new_core = backend(tmp_path)
    assert isinstance(new_core(), SessionJournalCore)

    first = _Run(tmp_path)
    await first.start(list(_GUARDED), core=new_core())
    events = await first.ask()
    assert events[-1].msg.kind == "turn_suspended", events[-1].msg.data
    thread_id = first.engine.thread_id
    await first.pool.close()  # 等人期间释放：Session 分离而不是终结

    # 另一个「进程」：新的 core 实例接在同一份存储上，接管后答复
    second = _Run(tmp_path)
    second.executed = first.executed
    await second.start([SimTurn(text="完成")], resume_thread_id=thread_id, core=new_core())
    done = await second.resume(c1={"granted": True})
    assert done[-1].msg.kind == "turn_completed", done[-1].msg.data
    assert second.executed == ["guarded:k1"]
    await second.pool.close()

    reader = new_core()
    envelopes = [envelope async for envelope in reader.load(_SESSION)]
    types = [envelope.record_type for envelope in envelopes]
    for expected in (
        "session_started", "turn_suspended", "session_detached", "writer_takeover",
        "resume_accepted", "resume_applied", "tool_outcome_committed", "session_ended",
    ):
        assert expected in types, (expected, types)
    assert types.index("session_detached") < types.index("writer_takeover")
    assert [e.writer_epoch for e in envelopes][-1] == 2
    # 整条链可校验、没有悬空的 effect
    verification = verify_envelopes(envelopes, session_id=_SESSION)
    assert verification.committed_tail_seq == len(envelopes)
    assert find_unsettled_effects(envelopes) == ()
    # Journal 不在本机：目录没有被创建
    assert not (tmp_path / "journal").exists()


async def test_a_live_writer_on_another_backend_still_fences_a_second_pool(tmp_path: Path) -> None:
    """writer 存活时第二个 pool 拿不到同一个 Session（fencing 由后端提供，内核如实上报）。"""
    from taifeng.loop.audit_bootstrap import AuditEngineCreationError

    new_core = _memory_cores(tmp_path)
    first = _Run(tmp_path)
    await first.start(list(_GUARDED), core=new_core())
    intruder = _Run(tmp_path)
    with pytest.raises(AuditEngineCreationError):
        await intruder.start([SimTurn(text="x")], core=new_core())
    # 原 writer 不受影响
    events = await first.ask()
    assert events[-1].msg.kind == "turn_suspended"
    await first.pool.close()
