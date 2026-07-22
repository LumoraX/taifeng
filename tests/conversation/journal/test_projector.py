"""SessionJournal durable ack 驱动的 conversation 投影测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import taifeng.conversation.journal as journal_package
from taifeng.conversation.journal.canonical import model_canonical_data
from taifeng.conversation.journal.framing import encode_batch
from taifeng.conversation.journal.models import (
    ActorRef,
    JournalAck,
    JournalEnvelope,
    JournalRecord,
)
from taifeng.conversation.journal.projector import (
    JournalConversationProjector,
    ProjectionOrderError,
)
from taifeng.conversation.journal.records import serialize_response_item
from taifeng.conversation.models import ResponseItem, assistant_message, user_message
from taifeng.conversation.transcript import JsonlMessageStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
_ZERO_HASH = "0" * 64


def _conversation_record(item: ResponseItem, *, record_id: str) -> JournalRecord:
    """把测试 item 包装成明确的 conversation_item record。"""
    payload = serialize_response_item(item, source_record_id=f"source_{record_id}")
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="conversation_item",
        actor=ActorRef(kind="system", source="test"),
        payload=model_canonical_data(payload),
        thread_id=item.thread_id,
    )


def _domain_record(*, record_id: str) -> JournalRecord:
    """构造 ack 内允许存在的非 conversation 领域 record。"""
    return JournalRecord(
        session_id="ses_1",
        record_id=record_id,
        record_type="turn_completed",
        actor=ActorRef(kind="system", source="test"),
        payload={"payload_version": 1, "status": "complete"},
        thread_id="thr_1",
    )


def _encoded(
    records: tuple[JournalRecord, ...], *, expected_seq: int = 3
) -> tuple[tuple[JournalEnvelope, ...], JournalAck]:
    """通过真实 frame codec 构造匹配的 envelopes/ack。"""
    batch = encode_batch(
        records,
        batch_id=f"batch_{expected_seq}_{len(records)}",
        expected_seq=expected_seq,
        writer_epoch=2,
        previous_hash=_ZERO_HASH,
        recorded_at=_NOW,
    )
    return batch.envelopes, batch.ack


class _MemoryProjectionStore:
    """可观察写效果并注入部分失败的最小投影 store。"""

    def __init__(self) -> None:
        self.items: dict[str, list[ResponseItem]] = {}
        self.append_calls = 0
        self.create_calls = 0
        self.fail_after_first_once = False
        self.fail_before_write_once = False
        self.fail_threads: set[str] = set()
        self.fail_load_threads: set[str] = set()
        self._projection_locks: dict[str, anyio.Lock] = {}

    def projection_lock(self, thread_id: str) -> anyio.Lock:
        """让同一 fake store 上的多个 projector 共享 thread 锁。"""
        return self._projection_locks.setdefault(thread_id, anyio.Lock())

    async def ensure_projection_thread(self, thread_id: str) -> None:
        """内存 fake 不需要修复 metadata 文件。"""

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """记录 bootstrap 效果。"""
        self.create_calls += 1
        if thread_id in self.items:
            raise FileExistsError(thread_id)
        self.items[thread_id] = []
        return thread_id

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """按需在首条写入后失败，模拟非原子 materialization。"""
        self.append_calls += 1
        if self.fail_before_write_once:
            self.fail_before_write_once = False
            raise OSError("injected pre-write projection failure")
        if items and items[0].thread_id in self.fail_threads:
            raise OSError("injected projection failure")
        if self.fail_after_first_once:
            self.fail_after_first_once = False
            first = items[0]
            self.items.setdefault(first.thread_id, []).append(first)
            raise OSError("injected partial projection failure")
        for item in items:
            self.items.setdefault(item.thread_id, []).append(item)

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """返回当前持久化 history 的异步快照。"""
        if thread_id in self.fail_load_threads:
            raise OSError("injected projection load failure")
        snapshot = list(self.items.get(thread_id, ()))

        async def _items() -> AsyncIterator[ResponseItem]:
            for item in snapshot:
                yield item

        return _items()


class _YieldingProjectionStore(_MemoryProjectionStore):
    """append 前让出调度，稳定暴露 load-then-append 并发竞争。"""

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """让并发 project 有机会同时观察旧 history。"""
        self.append_calls += 1
        await anyio.lowlevel.checkpoint()
        for item in items:
            self.items.setdefault(item.thread_id, []).append(item)


class _YieldingJsonlMessageStore(JsonlMessageStore):
    """使用真实 JSONL，仅在 append 前让出调度以放大跨 projector 竞争。"""

    def __init__(self, threads_dir: Path) -> None:
        super().__init__(threads_dir)
        self.append_calls = 0

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """让两个 projector 有机会在落盘前分别完成 history 检查。"""
        self.append_calls += 1
        await anyio.lowlevel.checkpoint()
        await super().append_batch(items)


class _FailingDirectory:
    """在 JSONL exclusive-create 后拒绝 metadata 注册。"""

    def __init__(self) -> None:
        self.upsert_calls = 0

    async def upsert_metadata(self, metadata: object) -> None:
        """模拟 derived directory 暂时不可用。"""
        self.upsert_calls += 1
        raise OSError("injected directory failure")

    async def close(self) -> None:
        """满足 store close 路径。"""


async def _history(store: _MemoryProjectionStore, thread_id: str) -> list[ResponseItem]:
    """收集 fake store 的异步 history。"""
    return [item async for item in await store.load_thread(thread_id)]


async def _create_explicit_projection(store: JsonlMessageStore) -> str:
    """用完整审计 metadata 创建固定测试 projection。"""
    return await store.create_projection_thread(
        thread_id="thr_explicit",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_1",
            "journal_schema_version": 1,
        },
    )


@pytest.mark.anyio
async def test_explicit_thread_id_bootstrap_writes_metadata_and_directory(
    tmp_path: Path,
) -> None:
    """default JSONL 投影必须用调用方 id 和审计 metadata 创建文件及索引。"""
    store = JsonlMessageStore(tmp_path)
    extra = {
        "audit_required": True,
        "journal_session_id": "ses_1",
        "journal_schema_version": 1,
        "custom": "kept",
    }

    created = await store.create_projection_thread(
        thread_id="thr_explicit",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra=extra,
    )

    first_line = (tmp_path / "thr_explicit.jsonl").read_text(encoding="utf-8").splitlines()[0]
    metadata = json.loads(first_line)
    indexed = await store._directory.get_metadata("thr_explicit")  # noqa: SLF001
    assert created == "thr_explicit"
    assert metadata["thread_id"] == "thr_explicit"
    assert metadata["extra"] == {"cwd": "/work", **extra}
    assert indexed is not None
    assert indexed.thread_id == "thr_explicit"
    assert indexed.extra == {"cwd": "/work", **extra}
    await store.close()


@pytest.mark.anyio
async def test_explicit_thread_id_bootstrap_rejects_duplicate_without_overwrite(
    tmp_path: Path,
) -> None:
    """重复显式 id 必须 exclusive-create，保留原 metadata。"""
    store = JsonlMessageStore(tmp_path)
    await store.create_projection_thread(
        thread_id="thr_explicit",
        cwd=None,
        entry_skill_id="general",
        source="system",
        extra={"audit_required": True},
    )
    before = (tmp_path / "thr_explicit.jsonl").read_bytes()

    with pytest.raises(FileExistsError):
        await store.create_projection_thread(
            thread_id="thr_explicit",
            cwd=None,
            entry_skill_id="other",
            source="user",
            extra={"audit_required": False},
        )

    assert (tmp_path / "thr_explicit.jsonl").read_bytes() == before
    await store.close()


@pytest.mark.anyio
async def test_metadata_registration_failure_keeps_rebuildable_file_and_rejects_retry(
    tmp_path: Path,
) -> None:
    """derived directory 失败时保留自包含 JSONL，传播错误且后续不得覆盖该文件。"""
    store = JsonlMessageStore(tmp_path)
    directory = _FailingDirectory()
    store._directory = directory  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(OSError, match="directory failure"):
        await _create_explicit_projection(store)

    path = tmp_path / "thr_explicit.jsonl"
    metadata = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert metadata["thread_id"] == "thr_explicit"
    assert metadata["extra"]["audit_required"] is True
    with pytest.raises(FileExistsError):
        await _create_explicit_projection(store)
    assert directory.upsert_calls == 1
    await store.close()


@pytest.mark.anyio
async def test_legacy_create_thread_still_generates_an_id(tmp_path: Path) -> None:
    """新增显式路径不能改变 legacy create_thread 的生成 id 行为。"""
    store = JsonlMessageStore(tmp_path)

    first = await store.create_thread(entry_skill_id="general")
    second = await store.create_thread(entry_skill_id="general")

    assert first.startswith("thr_")
    assert second.startswith("thr_")
    assert first != second
    await store.close()


@pytest.mark.anyio
async def test_projector_bootstrap_requires_caller_supplied_audit_metadata() -> None:
    """projector 不得自行猜测 Journal 审计 marker。"""
    store = _MemoryProjectionStore()
    projector = JournalConversationProjector(store)

    with pytest.raises(ProjectionOrderError, match="audit metadata"):
        await projector.bootstrap_thread(
            thread_id="thr_1",
            cwd=None,
            entry_skill_id="general",
            source="system",
            extra={"audit_required": True, "journal_session_id": "ses_1"},
        )

    assert store.create_calls == 0


@pytest.mark.anyio
async def test_projector_bootstrap_forwards_complete_audit_metadata() -> None:
    """完整审计 marker 必须原样交给投影 store，且返回预分配 id。"""
    store = _MemoryProjectionStore()
    projector = JournalConversationProjector(store)

    created = await projector.bootstrap_thread(
        thread_id="thr_1",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_1",
            "journal_schema_version": 1,
        },
    )

    assert created == "thr_1"
    assert store.create_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "seq_not_covered",
        "record_not_covered",
        "session_mismatch",
        "epoch_mismatch",
        "non_conversation",
        "invalid_payload",
        "unsorted",
        "duplicate_seq",
        "duplicate_record_id",
        "duplicate_ack_record_id",
        "ack_record_order",
        "thread_mismatch",
        "cross_thread",
    ],
)
async def test_contract_mismatch_is_rejected_before_store_writes(mutation: str) -> None:
    """顺序、ack、V1 或 thread 契约错误必须在全部 materialization 前失败。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(update={"id": "item_1"})
    second = user_message(text="two", thread_id="thr_1").model_copy(update={"id": "item_2"})
    envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _conversation_record(second, record_id="rec_2"),
        )
    )
    selected = list(envelopes)
    if mutation == "seq_not_covered":
        ack = ack.model_copy(update={"first_seq": 5, "last_seq": 5})
        selected = [envelopes[0]]
    elif mutation == "record_not_covered":
        ack = ack.model_copy(update={"record_ids": ("rec_other",)})
        selected = [envelopes[0]]
    elif mutation == "session_mismatch":
        ack = ack.model_copy(update={"session_id": "ses_other"})
    elif mutation == "epoch_mismatch":
        ack = ack.model_copy(update={"writer_epoch": 3})
    elif mutation == "non_conversation":
        selected[0] = selected[0].model_copy(update={"record_type": "turn_completed"})
    elif mutation == "invalid_payload":
        selected[0] = selected[0].model_copy(update={"payload": {"payload_version": 1}})
    elif mutation == "unsorted":
        selected.reverse()
    elif mutation == "duplicate_seq":
        selected[1] = selected[1].model_copy(update={"seq": selected[0].seq})
    elif mutation == "duplicate_record_id":
        selected[1] = selected[1].model_copy(update={"record_id": selected[0].record_id})
    elif mutation == "duplicate_ack_record_id":
        ack = ack.model_copy(update={"record_ids": ("rec_1", "rec_1", "rec_2")})
    elif mutation == "ack_record_order":
        ack = ack.model_copy(update={"record_ids": tuple(reversed(ack.record_ids))})
    elif mutation == "thread_mismatch":
        selected[0] = selected[0].model_copy(update={"thread_id": "thr_other"})
    elif mutation == "cross_thread":
        other = user_message(text="other", thread_id="thr_2").model_copy(update={"id": "item_2"})
        selected[1] = selected[1].model_copy(
            update={
                "thread_id": "thr_2",
                "payload": model_canonical_data(
                    serialize_response_item(other, source_record_id="source_rec_2")
                ),
            }
        )

    store = _MemoryProjectionStore()
    projector = JournalConversationProjector(store)

    with pytest.raises(ProjectionOrderError):
        await projector.project(tuple(selected), ack)

    assert store.append_calls == 0
    assert store.create_calls == 0


@pytest.mark.anyio
async def test_acknowledged_item_projects_and_roundtrips() -> None:
    """只有覆盖 ack 的 conversation_item 才能落入 transcript。"""
    item = assistant_message("answer", thread_id="thr_1", model="sim").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _MemoryProjectionStore()

    result = await JournalConversationProjector(store).project(envelopes, ack)

    assert result.thread_id == "thr_1"
    assert result.projected_seq == 4
    assert result.stale is False
    assert await _history(store, "thr_1") == [item]


@pytest.mark.anyio
async def test_same_apply_and_new_projector_replay_deduplicate_durable_item_id() -> None:
    """同实例与新实例重放都以落盘 item_id 去重。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _MemoryProjectionStore()
    first = JournalConversationProjector(store)

    await first.project(envelopes, ack)
    repeated = await first.project(envelopes, ack)
    restarted = await JournalConversationProjector(store).project(envelopes, ack)

    assert repeated.projected_seq == restarted.projected_seq == 4
    assert [entry.id for entry in await _history(store, "thr_1")] == ["item_1"]
    assert store.append_calls == 1


@pytest.mark.anyio
async def test_duplicate_item_id_in_one_batch_is_rejected_before_writes() -> None:
    """不同 envelope 不得复用 durable item_id，即便 Journal record id 不同。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    duplicate = user_message(text="changed", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _conversation_record(duplicate, record_id="rec_2"),
        )
    )
    store = _MemoryProjectionStore()

    with pytest.raises(ProjectionOrderError, match="item ids"):
        await JournalConversationProjector(store).project(envelopes, ack)

    assert store.append_calls == 0


@pytest.mark.anyio
async def test_existing_same_item_id_with_different_content_marks_stale() -> None:
    """item_id 只在内容完全相同时幂等；冲突 transcript 必须可观察为 stale。"""
    expected = user_message(text="expected", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    conflicting = expected.model_copy(update={"payload": {"text": "changed", "attachments": []}})
    envelopes, ack = _encoded((_conversation_record(expected, record_id="rec_1"),))
    store = _MemoryProjectionStore()
    store.items["thr_1"] = [conflicting]

    result = await JournalConversationProjector(store).project(envelopes, ack)

    assert result.stale is True
    assert result.projected_seq == 0
    assert result.failure_class == "item_id_conflict"
    assert store.append_calls == 0


@pytest.mark.anyio
async def test_replay_detects_reversed_durable_history_order() -> None:
    """ID 与内容都相同但物理 history 逆序时必须 stale，不能健康推进 watermark。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    second = user_message(text="two", thread_id="thr_1").model_copy(
        update={"id": "item_2", "created_at": _NOW}
    )
    envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _conversation_record(second, record_id="rec_2"),
        )
    )
    store = _MemoryProjectionStore()
    store.items["thr_1"] = [second, first]

    result = await JournalConversationProjector(store).project(envelopes, ack)

    assert result.stale is True
    assert result.projected_seq == 0
    assert result.failure_class == "projection_order_conflict"
    assert store.append_calls == 0


@pytest.mark.anyio
async def test_replay_rebuilds_deleted_transcript_despite_in_memory_watermark() -> None:
    """watermark 已前进后投影文件被重建为空，Journal 重放仍必须恢复内容。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _MemoryProjectionStore()
    projector = JournalConversationProjector(store)
    await projector.project(envelopes, ack)
    store.items["thr_1"].clear()

    replayed = await projector.project(envelopes, ack)

    assert replayed.stale is False
    assert replayed.projected_seq == 4
    assert await _history(store, "thr_1") == [item]
    assert store.append_calls == 2


@pytest.mark.anyio
async def test_real_jsonl_replay_recreates_audited_metadata_after_file_deletion(
    tmp_path: Path,
) -> None:
    """删除默认投影文件后，同一 projector 重放必须先恢复显式 id 的审计 metadata。"""
    store = JsonlMessageStore(tmp_path)
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id="thr_explicit",
        cwd="/work",
        entry_skill_id="general",
        source="system",
        extra={
            "audit_required": True,
            "journal_session_id": "ses_1",
            "journal_schema_version": 1,
        },
    )
    item = user_message(text="one", thread_id="thr_explicit").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    await projector.project(envelopes, ack)
    path = tmp_path / "thr_explicit.jsonl"
    path.unlink()

    replayed = await projector.project(envelopes, ack)

    lines = path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0])
    assert replayed.stale is False
    assert metadata["__meta__"] is True
    assert metadata["thread_id"] == "thr_explicit"
    assert metadata["extra"]["audit_required"] is True
    assert metadata["extra"]["journal_session_id"] == "ses_1"
    assert metadata["extra"]["journal_schema_version"] == 1
    assert len([item async for item in await store.load_thread("thr_explicit")]) == 1
    await store.close()


@pytest.mark.anyio
async def test_replay_repairs_missing_transcript_suffix_after_success() -> None:
    """已前进 watermark 不能掩盖 transcript 缺失的尾部 item。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    second = user_message(text="two", thread_id="thr_1").model_copy(
        update={"id": "item_2", "created_at": _NOW}
    )
    envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _conversation_record(second, record_id="rec_2"),
        )
    )
    store = _MemoryProjectionStore()
    projector = JournalConversationProjector(store)
    await projector.project(envelopes, ack)
    store.items["thr_1"].pop()

    replayed = await projector.project(envelopes, ack)

    assert replayed.stale is False
    assert [item.id for item in await _history(store, "thr_1")] == ["item_1", "item_2"]


@pytest.mark.anyio
async def test_concurrent_same_batch_is_serialized_per_thread() -> None:
    """并发重放不能让两个 load 都观察空 history 后重复 append。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _YieldingProjectionStore()
    projector = JournalConversationProjector(store)
    results = []

    async def _project() -> None:
        results.append(await projector.project(envelopes, ack))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_project)
        task_group.start_soon(_project)

    assert len(results) == 2
    assert [item.id for item in await _history(store, "thr_1")] == ["item_1"]
    assert store.append_calls == 1


@pytest.mark.anyio
async def test_two_projectors_share_real_store_thread_lock(tmp_path: Path) -> None:
    """两个 projector 共享真实 store 时不得同时 load 空 history 后重复追加。"""
    store = _YieldingJsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    item = user_message(text="one", thread_id="thr_explicit").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    first = JournalConversationProjector(store)
    second = JournalConversationProjector(store)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(first.project, envelopes, ack)
        task_group.start_soon(second.project, envelopes, ack)

    history = [item async for item in await store.load_thread("thr_explicit")]
    assert [item.id for item in history] == ["item_1"]
    assert store.append_calls == 1
    await store.close()


@pytest.mark.anyio
async def test_seq_gaps_preserve_journal_order_and_ignore_domain_ack_records() -> None:
    """conversation envelope 可跳过同 ack 的领域 record，但顺序仍由 seq 决定。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    second = user_message(text="two", thread_id="thr_1").model_copy(
        update={"id": "item_2", "created_at": _NOW}
    )
    all_envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _domain_record(record_id="rec_domain"),
            _conversation_record(second, record_id="rec_2"),
        )
    )
    conversation_envelopes = (all_envelopes[0], all_envelopes[2])
    store = _MemoryProjectionStore()

    result = await JournalConversationProjector(store).project(conversation_envelopes, ack)

    assert [item.id for item in await _history(store, "thr_1")] == ["item_1", "item_2"]
    assert result.projected_seq == 6


@pytest.mark.anyio
async def test_partial_materialization_failure_is_stale_and_replay_converges() -> None:
    """部分 append 失败不抛 Journal 错误，watermark 不前进，重放最终与干净投影一致。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    second = user_message(text="two", thread_id="thr_1").model_copy(
        update={"id": "item_2", "created_at": _NOW}
    )
    envelopes, ack = _encoded(
        (
            _conversation_record(first, record_id="rec_1"),
            _conversation_record(second, record_id="rec_2"),
        )
    )
    store = _MemoryProjectionStore()
    store.fail_after_first_once = True
    projector = JournalConversationProjector(store)

    stale = await projector.project(envelopes, ack)
    recovered = await projector.project(envelopes, ack)
    clean_store = _MemoryProjectionStore()
    await JournalConversationProjector(clean_store).project(envelopes, ack)

    assert stale.stale is True
    assert stale.projected_seq == 0
    assert stale.failure_class == "OSError"
    assert recovered.stale is False
    assert recovered.projected_seq == 5
    assert await _history(store, "thr_1") == await _history(clean_store, "thr_1")


@pytest.mark.anyio
async def test_stale_gap_blocks_higher_seq_until_failed_batch_replays() -> None:
    """seq5 投影失败后 seq6 不能越过缺口；重放 seq5 后才允许按序收敛。"""
    fifth = user_message(text="five", thread_id="thr_1").model_copy(
        update={"id": "item_5", "created_at": _NOW}
    )
    sixth = user_message(text="six", thread_id="thr_1").model_copy(
        update={"id": "item_6", "created_at": _NOW}
    )
    envelopes_5, ack_5 = _encoded(
        (_conversation_record(fifth, record_id="rec_5"),), expected_seq=4
    )
    envelopes_6, ack_6 = _encoded(
        (_conversation_record(sixth, record_id="rec_6"),), expected_seq=5
    )
    store = _MemoryProjectionStore()
    store.fail_before_write_once = True
    projector = JournalConversationProjector(store)

    failed = await projector.project(envelopes_5, ack_5)
    blocked = await projector.project(envelopes_6, ack_6)

    assert failed.stale is True
    assert blocked == failed
    assert await _history(store, "thr_1") == []
    recovered = await projector.project(envelopes_5, ack_5)
    advanced = await projector.project(envelopes_6, ack_6)
    assert recovered.stale is False
    assert recovered.projected_seq == 5
    assert advanced.stale is False
    assert advanced.projected_seq == 6
    assert [item.id for item in await _history(store, "thr_1")] == ["item_5", "item_6"]


@pytest.mark.anyio
async def test_projection_load_failure_returns_stale_without_raising() -> None:
    """读取 durable transcript 失败同样属于 projection stale，不得逃逸成 Journal 故障。"""
    item = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    envelopes, ack = _encoded((_conversation_record(item, record_id="rec_1"),))
    store = _MemoryProjectionStore()
    store.fail_load_threads.add("thr_1")

    result = await JournalConversationProjector(store).project(envelopes, ack)

    assert result.stale is True
    assert result.projected_seq == 0
    assert result.failure_class == "OSError"


@pytest.mark.anyio
async def test_stale_watermark_is_isolated_per_thread() -> None:
    """一个 thread 的 projection stale 不得污染另一个 thread。"""
    first = user_message(text="one", thread_id="thr_1").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    second = user_message(text="two", thread_id="thr_2").model_copy(
        update={"id": "item_2", "created_at": _NOW}
    )
    envelopes_1, ack_1 = _encoded((_conversation_record(first, record_id="rec_1"),))
    record_2 = _conversation_record(second, record_id="rec_2").model_copy(
        update={"session_id": "ses_2"}
    )
    envelopes_2, ack_2 = _encoded((record_2,))
    store = _MemoryProjectionStore()
    store.fail_threads.add("thr_1")
    projector = JournalConversationProjector(store)

    stale = await projector.project(envelopes_1, ack_1)
    healthy = await projector.project(envelopes_2, ack_2)

    assert stale.stale is True
    assert stale.projected_seq == 0
    assert healthy.stale is False
    assert healthy.projected_seq == 4
    assert projector.state("thr_1").stale is True
    assert projector.state("thr_2").stale is False


def test_projector_exposes_no_arbitrary_response_item_append_api() -> None:
    """projector 只能消费 JournalEnvelope + JournalAck，不能接受 ResponseItem 双写。"""
    assert not hasattr(JournalConversationProjector, "append")
    assert not hasattr(JournalConversationProjector, "append_batch")


def test_projector_is_exported_only_from_experimental_journal_package() -> None:
    """实验 projector 可从 journal 包使用，但不要求 conversation 顶层暴露。"""
    assert journal_package.JournalConversationProjector is JournalConversationProjector
