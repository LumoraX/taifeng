"""``EnginePool.create(message_store=)``（ADR 0111）：会话主存可以是外部 ``MessageStore``。

测试只用稳定层的名字——这正是外部适配包的处境。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng import (
    AtomicBatchMessageStore,
    BatchAppendAck,
    BatchConflictError,
    MessageStore,
    ModelCapabilities,
    ResponseItem,
    SimClient,
    SimTurn,
    ThreadInfo,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path


class _MemoryStore:
    """进程内 ``MessageStore``：代表「数据库版」的外部实现。"""

    def __init__(self) -> None:
        self.threads: dict[str, list[ResponseItem]] = {}
        self.created: dict[str, dict[str, Any]] = {}
        self.closed = 0

    async def append(self, item: ResponseItem) -> None:
        self.threads.setdefault(item.thread_id, []).append(item)

    async def append_batch(self, items: list[ResponseItem]) -> None:
        for item in items:
            await self.append(item)

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        if thread_id not in self.threads:
            raise taifeng.ThreadNotFoundError(thread_id)
        items = list(self.threads[thread_id])

        async def iterate() -> AsyncIterator[ResponseItem]:
            for item in items:
                yield item

        return iterate()

    async def list_threads(self, *, cwd: str | None = None, limit: int = 50) -> list[ThreadInfo]:
        now = datetime.now(UTC)
        return [
            ThreadInfo(
                thread_id=thread_id, created_at=now, last_activity_at=now, item_count=len(items),
                entry_skill_id=self.created[thread_id].get("entry_skill_id"),
            )
            for thread_id, items in list(self.threads.items())[:limit]
        ]

    async def create_thread(
        self, *, cwd: str | None = None, entry_skill_id: str | None = None,
        source: str | None = None, extra: dict[str, Any] | None = None,
    ) -> str:
        thread_id = f"thr_mem_{len(self.threads) + 1}"
        self.threads[thread_id] = []
        self.created[thread_id] = {"entry_skill_id": entry_skill_id, "source": source}
        return thread_id

    async def select_resume_path(self, cwd: str) -> str | None:
        return None

    async def close(self) -> None:
        self.closed += 1


class _AtomicMemoryStore(_MemoryStore):
    """再实现原子批量写：可以配 Responses 协议的模型客户端。"""

    def __init__(self) -> None:
        super().__init__()
        self.batches: dict[str, str] = {}

    async def append_atomic_batch(
        self, items: Sequence[ResponseItem], *, batch_id: str,
    ) -> BatchAppendAck:
        digest = hashlib.sha256(json.dumps(
            [item.model_dump(mode="json") for item in items], sort_keys=True, default=str,
        ).encode()).hexdigest()
        ids = tuple(item.id for item in items)
        if batch_id in self.batches:
            if self.batches[batch_id] != digest:
                raise BatchConflictError(batch_id)
            return BatchAppendAck(batch_id, digest, ids, already_committed=True)
        for item in items:
            await self.append(item)
        self.batches[batch_id] = digest
        return BatchAppendAck(batch_id, digest, ids)


def _skills(tmp_path: Path) -> Path:
    for name, front in (
        ("entry", "type: composite\nentry: true\nmodel: sim-model\nchild_skills: [helper]\n"),
        ("helper", "type: atomic\n"),
    ):
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\nversion: 1.0.0\n{front}---\n# {name}\n",
            encoding="utf-8",
        )
    return tmp_path / "skills"


async def _one_turn(engine: taifeng.AgentEngine, text: str) -> str:
    submission = await engine.submit(taifeng.UserMessage(text=text))
    async for event in engine.subscribe(submission):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            return str(event.msg.kind)
    raise AssertionError("turn never finished")


def test_memory_store_satisfies_the_protocols() -> None:
    assert isinstance(_MemoryStore(), MessageStore)
    assert not isinstance(_MemoryStore(), AtomicBatchMessageStore)
    assert isinstance(_AtomicMemoryStore(), AtomicBatchMessageStore)


async def test_conversation_lands_in_the_external_store_and_nowhere_else(tmp_path: Path) -> None:
    store = _MemoryStore()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), message_store=store, compressors=[],
        model_client=SimClient(turns=[SimTurn(text="你好")]),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="entry")
    assert await _one_turn(engine, "hi") == "turn_completed"

    kinds = [item.kind for item in store.threads[engine.thread_id]]
    assert kinds[:2] == ["user_message", "assistant_message"]
    assert store.created[engine.thread_id]["entry_skill_id"] == "entry"
    await pool.close()
    # 池拥有注入的 store：关闭时一并关闭，且只关一次
    assert store.closed == 1
    # 没有给存储目录，也就没有任何本机 transcript / 索引文件
    assert sorted(p.name for p in tmp_path.iterdir()) == ["skills"]


async def test_a_thread_is_resumed_from_the_external_store(tmp_path: Path) -> None:
    store = _MemoryStore()
    skills = _skills(tmp_path)
    first = await taifeng.EnginePool.create(
        skills_dir=skills, message_store=store, compressors=[],
        model_client=SimClient(turns=[SimTurn(text="第一轮")]),
    )
    engine = await first.get_or_create(session_id="s", entry_skill_id="entry")
    assert await _one_turn(engine, "one") == "turn_completed"
    thread_id = engine.thread_id
    await first.close()

    second = await taifeng.EnginePool.create(
        skills_dir=skills, message_store=store, compressors=[],
        model_client=SimClient(turns=[SimTurn(text="第二轮")]),
    )
    resumed = await second.get_or_create(
        session_id="s", entry_skill_id="entry", resume_thread_id=thread_id,
    )
    assert [i.kind for i in resumed.history_snapshot()][:2] == ["user_message", "assistant_message"]
    assert await _one_turn(resumed, "two") == "turn_completed"
    texts = [i.payload.get("text") for i in store.threads[thread_id] if i.kind == "assistant_message"]
    assert texts == ["第一轮", "第二轮"]
    await second.close()


async def test_index_hook_and_directory_work_with_an_external_store(tmp_path: Path) -> None:
    created: list[str] = []
    appended: list[int] = []

    class _Hook(taifeng.NoopIndexHook):
        async def on_thread_created(self, metadata: taifeng.ThreadMetadata) -> None:
            created.append(metadata.thread_id)

        async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
            appended.append(len(items))

    store = _MemoryStore()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), message_store=store, compressors=[],
        model_client=SimClient(turns=[SimTurn(text="ok")]),
        index_hook=_Hook(), thread_directory=taifeng.NullThreadDirectory(),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="entry")
    assert await _one_turn(engine, "hi") == "turn_completed"
    await pool.close()  # 关闭前等完后台 hook

    assert created == [engine.thread_id]
    assert sum(appended) == len(store.threads[engine.thread_id])


async def test_store_and_storage_dir_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="message_store"):
        await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), message_store=_MemoryStore(),
            storage_dir=tmp_path / "threads", model_client=SimClient(turns=[]),
        )


async def test_neither_store_nor_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="storage_dir"):
        await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), model_client=SimClient(turns=[]),
        )


async def test_an_object_that_is_not_a_message_store_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="MessageStore"):
        await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), message_store=object(),  # type: ignore[arg-type]
            model_client=SimClient(turns=[]),
        )


_RESPONSES = ModelCapabilities(
    input_modalities=frozenset({"text"}), provider="sim", protocol="responses",
)


async def test_responses_client_needs_an_atomic_external_store(tmp_path: Path) -> None:
    """配 Responses 协议的模型客户端，外部 store 必须实现原子批量写；缺了在构造时就拒绝。"""
    store = _MemoryStore()
    with pytest.raises(taifeng.LLMError, match="AtomicBatchMessageStore"):
        await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), message_store=store, compressors=[],
            model_client=SimClient(turns=[SimTurn(text="x")], capabilities=_RESPONSES),
        )
    # 拒绝时 store 还是调用方的：池没有建成，不替调用方关
    assert store.closed == 0


async def test_responses_client_commits_through_the_atomic_batch(tmp_path: Path) -> None:
    # 模拟器只说 Chat 协议；Responses 的终态事件用内核自己的测试替身回放
    from tests.loop.test_openai_responses_durable import _done, _ResponsesClient

    store = _AtomicMemoryStore()
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), message_store=store, compressors=[],
        model_client=_ResponsesClient([_done(
            [{"type": "message", "output_index": 0, "text": "原子提交"}], end_turn=True,
        )]),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="entry")
    assert await _one_turn(engine, "hi") == "turn_completed"
    await pool.close()

    # 一次采样的终态输出经原子批量写落进外部 store
    assert len(store.batches) == 1
    texts = [i.payload.get("text") for i in store.threads[engine.thread_id]
             if i.kind == "assistant_message"]
    assert texts == ["原子提交"]


async def test_audit_mode_still_requires_the_default_store(tmp_path: Path) -> None:
    """审计模式的对话投影只支持默认 JSONL store：外部 store 被显式拒绝，不是悄悄不投影。"""
    from taifeng.experimental import AuditCapabilityError, AuditConfig, JsonlSessionJournalCore
    from taifeng.llm.audit import AttemptObservableClientAdapter

    store = _AtomicMemoryStore()
    with pytest.raises(AuditCapabilityError) as raised:
        await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), message_store=store, compressors=[],
            model_client=AttemptObservableClientAdapter(
                SimClient(turns=[]), provider="sim", default_model="sim-model"),
            audit=AuditConfig(
                journal_core=JsonlSessionJournalCore(tmp_path / "journal"), writer_id="w",
                max_attachment_bytes=1024, max_total_attachment_bytes=4096,
            ),
        )
    assert "audit_custom_store_unsupported" in str(raised.value)
    # 池没有建成：注入的 store 仍归调用方，不替它关
    assert store.closed == 0
