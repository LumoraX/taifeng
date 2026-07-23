"""Conversation projector 的物理 materialization target 回归测试。"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal import materialization as materialization_module
from taifeng.conversation.journal.materialization import (
    ProjectionFileIdentity,
    ProjectionLifecycleError,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.models import user_message
from taifeng.conversation.transcript import JsonlMessageStore, JsonlMessageWriter
from tests.conversation.journal.projector_test_support import (
    _NOW,
    _conversation_record,
    _create_explicit_projection,
    _encoded,
)

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.models import ResponseItem


def _one_item_batch() -> tuple[Any, Any]:
    """构造固定单 item durable batch。"""
    item = user_message(text="one", thread_id="thr_explicit").model_copy(
        update={"id": "item_1", "created_at": _NOW}
    )
    return _encoded((_conversation_record(item, record_id="rec_1"),))


class _YieldingStore(JsonlMessageStore):
    """在投影追加前让出调度，稳定放大跨 handle 竞争。"""

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """让两个 handle 都有机会先读到空 snapshot。"""
        await anyio.lowlevel.checkpoint()
        await super().append_batch(items)

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """在 audited append 边界让出调度。"""
        await anyio.lowlevel.checkpoint()
        await super().append_projection_batch(thread_id, items, expected_identity)


class _BlockingStore(JsonlMessageStore):
    """允许测试在投影写入阶段暂停 store handle。"""

    def __init__(self, threads_dir: Path) -> None:
        super().__init__(threads_dir)
        self.append_started = anyio.Event()
        self.allow_append = anyio.Event()

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """等待测试释放后才执行真实追加。"""
        self.append_started.set()
        await self.allow_append.wait()
        await super().append_batch(items)

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """在 audited append 边界等待测试释放。"""
        self.append_started.set()
        await self.allow_append.wait()
        await super().append_projection_batch(thread_id, items, expected_identity)


class _MutatingStore(JsonlMessageStore):
    """在 snapshot 与 audited append 之间删除或替换目标文件。"""

    def __init__(self, threads_dir: Path, *, mutation: str) -> None:
        super().__init__(threads_dir)
        self.mutation = mutation
        self.mutated = False

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """注入一次路径身份变化后再调用真实 audited append。"""
        if not self.mutated:
            self.mutated = True
            path = self._writer._thread_path(thread_id)  # noqa: SLF001
            if self.mutation == "delete":
                path.unlink()
            else:
                replacement = path.with_suffix(".replacement")
                metadata = path.read_text(encoding="utf-8").splitlines()[0]
                replacement.write_text(metadata + "\n", encoding="utf-8")
                replacement.replace(path)
        await super().append_projection_batch(thread_id, items, expected_identity)


@pytest.mark.anyio
async def test_store_handles_share_physical_target_lock(tmp_path: Path) -> None:
    """同一 resolved directory 的两个 store handle 只能追加一次。"""
    first_store = _YieldingStore(tmp_path)
    await _create_explicit_projection(first_store)
    second_store = _YieldingStore(tmp_path / ".")
    envelopes, ack = _one_item_batch()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(JournalConversationProjector(first_store).project, envelopes, ack)
        task_group.start_soon(JournalConversationProjector(second_store).project, envelopes, ack)

    history = [item async for item in await first_store.load_thread("thr_explicit")]
    assert [item.id for item in history] == ["item_1"]
    await first_store.close()
    await second_store.close()


@pytest.mark.anyio
async def test_close_waits_for_admitted_projection(tmp_path: Path) -> None:
    """close 进入 CLOSING 后必须等待该 handle 已准入的投影退出。"""
    store = _BlockingStore(tmp_path)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    close_returned = anyio.Event()

    async def _close() -> None:
        await store.close()
        close_returned.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(JournalConversationProjector(store).project, envelopes, ack)
        await store.append_started.wait()
        task_group.start_soon(_close)
        await anyio.lowlevel.checkpoint()
        assert not close_returned.is_set()
        store.allow_append.set()

    assert close_returned.is_set()


@pytest.mark.anyio
async def test_project_after_close_is_rejected(tmp_path: Path) -> None:
    """CLOSED handle 不得准入新的投影。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    await store.close()

    with pytest.raises(RuntimeError, match="closed"):
        await JournalConversationProjector(store).project(envelopes, ack)


@pytest.mark.anyio
async def test_final_close_releases_shared_projection_state(tmp_path: Path) -> None:
    """最后一个 handle 关闭后，新 target 不继承旧 watermark。"""
    first = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(first)
    envelopes, ack = _one_item_batch()
    projected = await JournalConversationProjector(first).project(envelopes, ack)
    assert first.projection_state("thr_explicit") == (projected, None)
    await first.close()

    reopened = JsonlMessageStore(tmp_path)
    assert reopened.projection_state("thr_explicit") == (None, None)
    await reopened.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "thread_id",
    ["/absolute", "nested/thread", r"nested\thread", ".", "..", "bad\x00id"],
)
async def test_explicit_projection_bootstrap_rejects_unsafe_thread_id(
    tmp_path: Path,
    thread_id: str,
) -> None:
    """显式投影 bootstrap 不得让 thread id 改写物理路径。"""
    store = JsonlMessageStore(tmp_path)
    with pytest.raises(ValueError, match="thread_id"):
        await store.create_projection_thread(
            thread_id=thread_id,
            cwd=None,
            entry_skill_id="general",
            source="system",
            extra={"audit_required": True},
        )
    await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "thread_id",
    ["/absolute", "nested/thread", r"nested\thread", ".", "..", "bad\x00id"],
)
async def test_legacy_writer_rejects_unsafe_thread_id(
    tmp_path: Path,
    thread_id: str,
) -> None:
    """legacy writer 的显式底层入口也不得逃逸 threads root。"""
    writer = JsonlMessageWriter(tmp_path)
    with pytest.raises(ValueError, match="thread_id"):
        await writer._create_thread_with_id(  # noqa: SLF001
            thread_id=thread_id,
            entry_skill_id="general",
            source="system",
        )


@pytest.mark.anyio
async def test_generated_and_legal_thread_ids_remain_compatible(tmp_path: Path) -> None:
    """安全校验不得破坏既有生成 id 与合法显式 id。"""
    writer = JsonlMessageWriter(tmp_path)
    generated = await writer.create_thread(entry_skill_id="general")
    explicit = await writer._create_thread_with_id(  # noqa: SLF001
        thread_id="thr_legal-01",
        entry_skill_id="general",
        source="system",
    )

    assert generated.startswith("thr_")
    assert explicit == "thr_legal-01"
    assert (tmp_path / f"{generated}.jsonl").is_file()
    assert (tmp_path / "thr_legal-01.jsonl").is_file()


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["missing", "corrupt", "mismatched"])
async def test_projection_repairs_metadata_and_preserves_items(
    tmp_path: Path,
    damage: str,
) -> None:
    """缺失、损坏或错配的首行 metadata 必须原子重建且保留 item lines。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    projector = JournalConversationProjector(store)
    await projector.project(envelopes, ack)
    path = tmp_path / "thr_explicit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    item_lines = lines[1:]
    if damage == "missing":
        damaged = item_lines
    elif damage == "corrupt":
        damaged = ["{broken", *item_lines]
    else:
        metadata = json.loads(lines[0])
        metadata["thread_id"] = "thr_other"
        damaged = [json.dumps(metadata), *item_lines]
    path.write_text("\n".join(damaged) + "\n", encoding="utf-8")

    replayed = await projector.project(envelopes, ack)

    repaired = path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(repaired[0])
    assert replayed.stale is False
    assert metadata["__meta__"] is True
    assert metadata["thread_id"] == "thr_explicit"
    assert metadata["extra"]["audit_required"] is True
    assert repaired[1:] == item_lines
    await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["delete", "replace"])
async def test_identity_change_before_append_returns_stale_without_item_only_file(
    tmp_path: Path,
    mutation: str,
) -> None:
    """snapshot 后路径被删除或换 inode 时不得向新文件报告健康写入。"""
    store = _MutatingStore(tmp_path, mutation=mutation)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    projector = JournalConversationProjector(store)

    stale = await projector.project(envelopes, ack)

    path = tmp_path / "thr_explicit.jsonl"
    assert stale.stale is True
    if path.exists():
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert first["__meta__"] is True
    replayed = await projector.project(envelopes, ack)
    assert replayed.stale is False
    assert [item.id async for item in await store.load_thread("thr_explicit")] == ["item_1"]
    await store.close()


@pytest.mark.anyio
async def test_normal_appends_reuse_projection_snapshot(tmp_path: Path) -> None:
    """正常 suffix append 只初次全扫，之后通过 identity stat 与 cache 增量推进。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    projector = JournalConversationProjector(store)
    for seq in range(4, 8):
        item = user_message(text=str(seq), thread_id="thr_explicit").model_copy(
            update={"id": f"item_{seq}", "created_at": _NOW}
        )
        envelopes, ack = _encoded(
            (_conversation_record(item, record_id=f"rec_{seq}"),),
            expected_seq=seq - 1,
        )
        result = await projector.project(envelopes, ack)
        assert result.stale is False

    assert store.projection_scan_count("thr_explicit") == 1
    await store.close()


@pytest.mark.anyio
async def test_external_mutation_invalidates_projection_snapshot(tmp_path: Path) -> None:
    """外部 size/mtime 变化必须使 cache 失效并触发下一次完整扫描。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    projector = JournalConversationProjector(store)
    await projector.project(envelopes, ack)
    assert store.projection_scan_count("thr_explicit") == 1
    path = tmp_path / "thr_explicit.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")

    replayed = await projector.project(envelopes, ack)

    assert replayed.stale is False
    assert store.projection_scan_count("thr_explicit") == 2
    await store.close()


@pytest.mark.anyio
async def test_active_physical_target_rejects_different_event_loop(tmp_path: Path) -> None:
    """同一活跃 target 不得把 anyio lock/cache 带到另一个 event loop。"""
    first = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(first)
    envelopes, ack = _one_item_batch()
    await JournalConversationProjector(first).project(envelopes, ack)
    second = JsonlMessageStore(tmp_path)

    async def _cross_loop() -> None:
        await JournalConversationProjector(second).project(envelopes, ack)

    with pytest.raises(ProjectionLifecycleError, match="different event loop/backend"):
        await anyio.to_thread.run_sync(lambda: anyio.run(_cross_loop))
    await first.close()
    await second.close()


@pytest.mark.anyio
async def test_lower_seq_after_watermark_is_stale_even_if_projection_was_deleted(
    tmp_path: Path,
) -> None:
    """观察过高 watermark 后，空物理文件不能把低 seq 伪装成合法首次投影。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    higher_item = user_message(text="six", thread_id="thr_explicit").model_copy(
        update={"id": "item_6", "created_at": _NOW}
    )
    higher, higher_ack = _encoded(
        (_conversation_record(higher_item, record_id="rec_6"),), expected_seq=5
    )
    projector = JournalConversationProjector(store)
    healthy = await projector.project(higher, higher_ack)
    assert healthy.projected_seq == 6
    (tmp_path / "thr_explicit.jsonl").unlink()
    lower_item = user_message(text="five", thread_id="thr_explicit").model_copy(
        update={"id": "item_5", "created_at": _NOW}
    )
    lower, lower_ack = _encoded(
        (_conversation_record(lower_item, record_id="rec_5"),), expected_seq=4
    )

    regressed = await projector.project(lower, lower_ack)

    assert regressed.stale is True
    assert regressed.failure_class == "sequence_regression"
    await store.close()


@pytest.mark.anyio
async def test_long_projection_scan_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整历史扫描变慢时，独立 ticker 仍能在 worker IO 期间推进。"""
    store = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(store)
    envelopes, ack = _one_item_batch()
    started = threading.Event()
    done = anyio.Event()
    ticks_during_scan = 0
    original = materialization_module._scan_projection_file  # noqa: SLF001

    def _slow_scan(path: Path, metadata: dict[str, object]) -> object:
        started.set()
        time.sleep(0.05)
        return original(path, metadata)

    monkeypatch.setattr(materialization_module, "_scan_projection_file", _slow_scan)

    async def _ticker() -> None:
        nonlocal ticks_during_scan
        while not done.is_set():
            if started.is_set():
                ticks_during_scan += 1
            await anyio.lowlevel.checkpoint()

    async def _project() -> None:
        await JournalConversationProjector(store).project(envelopes, ack)
        done.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_ticker)
        task_group.start_soon(_project)

    assert ticks_during_scan > 0
    await store.close()


@pytest.mark.anyio
async def test_final_release_allows_rebind_on_another_event_loop(tmp_path: Path) -> None:
    """最后一个 handle 关闭并销毁 target 后，新 handle 可绑定其他 event loop。"""
    first = JsonlMessageStore(tmp_path)
    await _create_explicit_projection(first)
    envelopes, ack = _one_item_batch()
    await JournalConversationProjector(first).project(envelopes, ack)
    await first.close()

    async def _replay_after_rebind() -> bool:
        reopened = JsonlMessageStore(tmp_path)
        result = await JournalConversationProjector(reopened).project(envelopes, ack)
        await reopened.close()
        return result.stale

    stale = await anyio.to_thread.run_sync(lambda: anyio.run(_replay_after_rebind))
    assert stale is False


@pytest.mark.anyio
async def test_same_thread_id_in_different_directories_is_isolated(tmp_path: Path) -> None:
    """不同 resolved roots 的锁、state、cache 与失败不得互相污染。"""
    failed = _MutatingStore(tmp_path / "failed", mutation="delete")
    healthy = JsonlMessageStore(tmp_path / "healthy")
    await _create_explicit_projection(failed)
    await _create_explicit_projection(healthy)
    envelopes, ack = _one_item_batch()

    stale = await JournalConversationProjector(failed).project(envelopes, ack)
    projected = await JournalConversationProjector(healthy).project(envelopes, ack)

    assert stale.stale is True
    assert projected.stale is False
    assert [item.id async for item in await healthy.load_thread("thr_explicit")] == ["item_1"]
    await failed.close()
    await healthy.close()
