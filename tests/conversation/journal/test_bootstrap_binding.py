"""Audited projection bootstrap 的 prewrite Session binding 回归测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal.materialization import ProjectionLifecycleError
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.transcript import JsonlMessageStore
from tests.conversation.journal.projector_test_support import _FailingDirectory

if TYPE_CHECKING:
    from pathlib import Path


async def _bootstrap(
    store: JsonlMessageStore,
    session_id: str,
) -> str:
    """以固定 thread id 创建指定 Session 的 audited projection。"""
    return await JournalConversationProjector(store).bootstrap_thread(
        thread_id="thr_explicit",
        cwd=None,
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": session_id,
            "journal_schema_version": 1,
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize("bootstrap_handle", ["same", "other"])
@pytest.mark.parametrize("file_state", ["existing", "deleted"])
async def test_conflicting_bootstrap_binding_rejects_before_file_or_directory_write(
    tmp_path: Path,
    bootstrap_handle: str,
    file_state: str,
) -> None:
    """active target 绑定 A 后，B bootstrap 必须在任何持久写前拒绝。"""
    first_store = JsonlMessageStore(tmp_path)
    await _bootstrap(first_store, "ses_A")
    second_store = (
        JsonlMessageStore(tmp_path) if bootstrap_handle == "other" else first_store
    )
    path = tmp_path / "thr_explicit.jsonl"
    if file_state == "deleted":
        path.unlink()
    before_exists = path.exists()
    before_bytes = path.read_bytes() if before_exists else None
    before_metadata = await first_store._directory.get_metadata(  # noqa: SLF001
        "thr_explicit"
    )
    before_state = first_store.projection_state("thr_explicit")
    before_scans = first_store.projection_scan_count("thr_explicit")

    with pytest.raises(ProjectionLifecycleError, match="Journal Session"):
        await _bootstrap(second_store, "ses_B")

    after_metadata = await first_store._directory.get_metadata(  # noqa: SLF001
        "thr_explicit"
    )
    assert path.exists() is before_exists
    assert (path.read_bytes() if path.exists() else None) == before_bytes
    assert after_metadata == before_metadata
    assert first_store._projection_target.expected_session_id(  # noqa: SLF001
        "thr_explicit"
    ) == "ses_A"
    assert first_store.projection_state("thr_explicit") == before_state
    assert first_store.projection_scan_count("thr_explicit") == before_scans
    if second_store is not first_store:
        await second_store.close()
    await first_store.close()


@pytest.mark.anyio
async def test_failed_duplicate_bootstrap_rolls_back_new_target_reservation(
    tmp_path: Path,
) -> None:
    """新 target 的 writer 未产出持久 identity 时必须撤销本次 reservation。"""
    original = JsonlMessageStore(tmp_path)
    await _bootstrap(original, "ses_A")
    await original.close()
    reopened = JsonlMessageStore(tmp_path)
    before = (tmp_path / "thr_explicit.jsonl").read_bytes()

    with pytest.raises(FileExistsError):
        await _bootstrap(reopened, "ses_A")

    assert (tmp_path / "thr_explicit.jsonl").read_bytes() == before
    assert reopened._projection_target.expected_session_id(  # noqa: SLF001
        "thr_explicit"
    ) is None
    await reopened.close()


@pytest.mark.anyio
async def test_directory_failure_keeps_file_identity_binding_and_rejects_retarget(
    tmp_path: Path,
) -> None:
    """JSONL 已成功后 directory 失败仍保留 self-contained file 与 matching binding。"""
    store = JsonlMessageStore(tmp_path)
    real_directory = store._directory  # noqa: SLF001
    failing_directory = _FailingDirectory()
    store._directory = failing_directory  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(OSError, match="directory failure"):
        await _bootstrap(store, "ses_A")

    path = tmp_path / "thr_explicit.jsonl"
    before = path.read_bytes()
    assert store._projection_target.expected_session_id(  # noqa: SLF001
        "thr_explicit"
    ) == "ses_A"
    store._directory = real_directory  # noqa: SLF001
    with pytest.raises(ProjectionLifecycleError, match="Journal Session"):
        await _bootstrap(store, "ses_B")
    with pytest.raises(FileExistsError):
        await _bootstrap(store, "ses_A")

    assert path.read_bytes() == before
    rebuilt = await real_directory.get_metadata("thr_explicit")
    assert rebuilt is not None
    assert rebuilt.extra["journal_session_id"] == "ses_A"
    assert failing_directory.upsert_calls == 1
    assert store._projection_target.expected_session_id(  # noqa: SLF001
        "thr_explicit"
    ) == "ses_A"
    await store.close()
