"""EnginePool strict audit Journal-first bootstrap 与 downgrade 边界测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import taifeng.loop.audit_bootstrap as audit_bootstrap_module
import taifeng.loop.pool as pool_module
from taifeng.conversation.journal.canonical import canonical_bytes
from taifeng.conversation.journal.models import (
    Durability,
    JournalAck,
    JournalRecord,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import (
    AttemptObservableModelClient,
    AuditCapabilityError,
    AuditConfig,
)
from taifeng.loop.pool import EnginePool
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.client import ModelClientSession
    from taifeng.loop.cancellation import CancellationToken

_HASH = "0" * 64


class _ObservedClient(AttemptObservableModelClient):
    """显式满足 Task 5 nominal observer-aware 边界的无网络 client。"""

    def __init__(self) -> None:
        self._inner = SimClient(turns=[SimTurn(text="unused")])

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """委派普通 Sim session。"""
        return self._inner.session(cancel=cancel, model=model)

    def session_with_attempt_observer(
        self,
        *,
        cancel: CancellationToken,
        attempt_observer: object,
        model: str | None = None,
    ) -> ModelClientSession:
        """Task 7 前只证明 observer 注入点存在。"""
        del attempt_observer
        return self._inner.session(cancel=cancel, model=model)


class _Registry:
    """返回固定 immutable snapshot 的最小 registry。"""

    def __init__(self) -> None:
        atomic = SkillDefinition(
            id="child",
            name="child",
            description="child",
            version="1",
            body="# child",
            body_path=Path("/child/SKILL.md"),
            type="atomic",
        )
        entry = SkillDefinition(
            id="entry",
            name="entry",
            description="entry",
            version="1",
            body="# entry",
            body_path=Path("/entry/SKILL.md"),
            type="composite",
            entry=True,
            child_skills=frozenset({"child"}),
        )
        self._snapshot = SkillSnapshot(version=1, skills=(atomic, entry))

    def snapshot(self) -> SkillSnapshot:
        """返回固定 snapshot。"""
        return self._snapshot

    def get(self, skill_id: str) -> SkillDefinition | None:
        """按 id 查询固定 snapshot。"""
        return self._snapshot.get(skill_id)


class _JournalCore:
    """记录初始化、terminal append 与 lease close 的可注入 Journal fake。"""

    def __init__(
        self,
        events: list[str],
        *,
        fail_create: BaseException | None = None,
        fail_terminal: BaseException | None = None,
    ) -> None:
        self.events = events
        self.fail_create = fail_create
        self.fail_terminal = fail_terminal
        self.descriptors: list[SessionDescriptor] = []
        self.appended: list[tuple[JournalRecord, ...]] = []
        self.close_calls = 0
        self.global_close_calls = 0

    async def create_session(
        self,
        descriptor: SessionDescriptor,
    ) -> SessionCreateResult:
        """返回覆盖初始化三记录的 definite durable ack。"""
        self.events.append("journal_create")
        self.descriptors.append(descriptor)
        if self.fail_create is not None:
            raise self.fail_create
        return SessionCreateResult(
            lease=SessionLease(
                session_id=descriptor.session_id,
                writer_id=descriptor.writer_id,
                writer_epoch=1,
                lease_id=f"{descriptor.session_id}:lease",
            ),
            ack=JournalAck(
                session_id=descriptor.session_id,
                first_seq=1,
                last_seq=3,
                record_ids=(
                    f"{descriptor.creation_operation_id}:session_started",
                    f"{descriptor.creation_operation_id}:thread_created",
                    f"{descriptor.creation_operation_id}:thread_bound",
                ),
                tail_hash=_HASH,
                writer_epoch=1,
                durability=Durability.COMMITTED,
            ),
        )

    async def append_batch(
        self,
        records: tuple[JournalRecord, ...],
        *,
        lease: SessionLease,
        expected_seq: int,
    ) -> JournalAck:
        """记录 terminal batch，或注入 terminal uncertainty。"""
        del lease
        self.events.append("journal_terminal")
        self.appended.append(records)
        if self.fail_terminal is not None:
            raise self.fail_terminal
        return JournalAck(
            session_id=records[0].session_id,
            first_seq=expected_seq + 1,
            last_seq=expected_seq + len(records),
            record_ids=tuple(record.record_id for record in records),
            tail_hash=_HASH,
            writer_epoch=1,
            durability=Durability.COMMITTED,
        )

    async def close_session(self, lease: SessionLease) -> None:
        """只记录 per-session lease close。"""
        self.events.append("lease_close")
        self.close_calls += 1
        assert lease.lease_id.endswith(":lease")

    async def close(self) -> None:
        """若 EnginePool 错误关闭 caller-owned core，测试立即记录。"""
        self.global_close_calls += 1


class _SpyStore(JsonlMessageStore):
    """默认 JSONL store 的 bootstrap/load effect spy。"""

    def __init__(
        self,
        root: Path,
        events: list[str],
        *,
        fail_projection: BaseException | None = None,
    ) -> None:
        super().__init__(root)
        self.events = events
        self.fail_projection = fail_projection
        self.load_calls = 0

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """记录 audited projection bootstrap effect。"""
        self.events.append("projection")
        if self.fail_projection is not None:
            raise self.fail_projection
        return await super().create_projection_thread(
            thread_id=thread_id,
            cwd=cwd,
            entry_skill_id=entry_skill_id,
            source=source,
            extra=extra,
        )

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """记录 legacy history load，供 downgrade-before-load 断言。"""
        self.load_calls += 1
        return await super().load_thread(thread_id)


class _EngineSpy:
    """只观察构造/warmup/run，不执行真实 turn runtime。"""

    def __init__(self, *, events: list[str], **kwargs: Any) -> None:
        events.append("engine_construct")
        self._events = events
        self.thread_id = kwargs["thread_id"]
        self.session_id = kwargs["session_id"]
        self._registry_ref: object | None = None

    async def warmup_engine_scope(self) -> None:
        """记录 warmup。"""
        self._events.append("warmup")

    async def run(self, cancel: CancellationToken) -> None:
        """记录 actor task 首次获得调度。"""
        del cancel
        self._events.append("actor_run")

    def has_live_spawns(self) -> bool:
        """测试 engine 没有 detached work。"""
        return False

    async def shutdown(self) -> None:
        """测试 teardown 无额外效果。"""
        self._events.append("engine_shutdown")

    def introspect(self) -> dict[str, object]:
        """返回最小 pool introspection。"""
        return {}


def _config(core: _JournalCore) -> AuditConfig:
    """构造 strict audit 配置。"""
    return AuditConfig(
        journal_core=core,
        writer_id="writer-1",
        max_attachment_bytes=1024,
        max_total_attachment_bytes=4096,
    )


def _pool(
    tmp_path: Path,
    core: _JournalCore,
    events: list[str],
    *,
    store: JsonlMessageStore | None = None,
    hooks: object | None = None,
) -> EnginePool:
    """直接构造无 built-in tools 的合法最小 audited pool。"""
    actual_store = store or JsonlMessageStore(tmp_path / "threads")
    return EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=_ObservedClient(),
        store=actual_store,
        tool_registry=ToolRegistry(),
        compressors=[],
        hooks=hooks,
        audit=_config(core),
    )


@pytest.mark.asyncio
async def test_audited_bootstrap_is_journal_first_and_injects_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journal/projection 成功后才构造、warmup、启动并缓存 Engine。"""
    events: list[str] = []
    core = _JournalCore(events)
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    original_bootstrap = JournalConversationProjector.bootstrap_thread

    async def _spy_bootstrap(
        projector: JournalConversationProjector,
        **kwargs: Any,
    ) -> str:
        events.append("projection")
        return await original_bootstrap(projector, **kwargs)

    monkeypatch.setattr(
        audit_bootstrap_module.JournalConversationProjector,
        "bootstrap_thread",
        _spy_bootstrap,
    )
    pool = _pool(tmp_path, core, events)

    engine = await pool.get_or_create(
        session_id="ses-1",
        entry_skill_id="entry",
        cwd="/stable/cwd",
    )
    await asyncio.sleep(0)

    assert events == [
        "journal_create",
        "projection",
        "engine_construct",
        "warmup",
        "actor_run",
    ]
    descriptor = core.descriptors[0]
    assert descriptor.creation_operation_id == "ses-1:create"
    assert descriptor.root_thread.thread_id == engine.thread_id
    assert descriptor.root_thread.entry_skill_id == "entry"
    assert descriptor.root_thread.source == "session:ses-1"
    assert descriptor.root_thread.extra == {"cwd": "/stable/cwd"}
    assert descriptor.config == {
        "audit_required": True,
        "journal_schema_version": 1,
        "max_attachment_bytes": 1024,
        "max_total_attachment_bytes": 4096,
        "strict_mode": "session_journal_business_v1",
    }
    canonical_bytes(descriptor.config)
    assert engine._audit_state.coordinator.expected_seq == 3  # noqa: SLF001
    assert engine._audit_state.projector is not None  # noqa: SLF001
    assert pool._audit_sessions["ses-1"].thread_id == engine.thread_id  # noqa: SLF001
    with pytest.raises(AuditCapabilityError) as caught:
        await pool.get_or_create(
            session_id="ses-1",
            entry_skill_id="entry",
            resume_thread_id="rejected-on-audited-cache-hit",
        )
    assert caught.value.code == "audit_resume_unsupported"
    assert events.count("journal_create") == 1
    await pool.close()
    assert core.global_close_calls == 0


def test_static_gate_uses_actual_pool_dependencies_before_journal_effect(
    tmp_path: Path,
) -> None:
    """实际 hooks 不可由 AuditConfig shadow；门禁失败不触发 Journal。"""
    events: list[str] = []
    core = _JournalCore(events)

    with pytest.raises(ValueError) as caught:
        _pool(tmp_path, core, events, hooks=object())

    assert getattr(caught.value, "code", None) == "audit_hooks_unsupported"
    assert events == []


@pytest.mark.asyncio
async def test_public_create_validates_actual_unclassified_builtins(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """public factory 不为现有 built-ins 伪造 Task 8 metadata。"""
    events: list[str] = []
    core = _JournalCore(events)

    with pytest.raises(AuditCapabilityError) as caught:
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=tmp_path / "threads",
            model_client=_ObservedClient(),
            compressors=[],
            audit=_config(core),
        )

    assert caught.value.code == "audit_tool_metadata_incomplete"
    assert events == []


@pytest.mark.asyncio
async def test_uncached_audited_resume_uses_independent_session_gate(
    tmp_path: Path,
) -> None:
    """audit-required 新 Session 不允许绕过到 legacy resume bootstrap。"""
    events: list[str] = []
    core = _JournalCore(events)
    pool = _pool(tmp_path, core, events)

    with pytest.raises(AuditCapabilityError) as caught:
        await pool.get_or_create(
            session_id="ses-resume",
            entry_skill_id="entry",
            resume_thread_id="legacy-thread",
        )

    assert caught.value.code == "audit_resume_unsupported"
    assert events == []
    await pool.close()


@pytest.mark.asyncio
async def test_journal_create_failure_has_no_projection_engine_or_fake_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 lease 时不得 projection、构造 Engine、finish 或 close。"""
    events: list[str] = []
    create_failure = OSError("secret=create-token")
    core = _JournalCore(events, fail_create=create_failure)
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    pool = _pool(tmp_path, core, events)

    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(session_id="ses-create-fail", entry_skill_id="entry")

    assert getattr(caught.value, "code", None) == "audit_engine_creation_failed"
    assert caught.value.__cause__ is create_failure
    assert "secret" not in str(caught.value)
    assert events == ["journal_create"]
    assert core.appended == []
    assert core.close_calls == 0
    assert "ses-create-fail" not in pool._engines  # noqa: SLF001
    assert "ses-create-fail" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_failure", "expected_audit_complete"),
    [
        (None, True),
        (OSError("secret=terminal-token"), False),
    ],
)
async def test_projection_failure_uses_unique_finish_and_single_lease_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_failure: BaseException | None,
    expected_audit_complete: bool,
) -> None:
    """projection 失败只走 coordinator.finish，结果保留 terminal/close 两维语义。"""
    events: list[str] = []
    projection_failure = OSError("secret=projection-token")
    core = _JournalCore(events, fail_terminal=terminal_failure)
    store = JsonlMessageStore(tmp_path / "threads")
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    async def _fail_projection(
        projector: JournalConversationProjector,
        **kwargs: Any,
    ) -> str:
        del projector, kwargs
        events.append("projection")
        raise projection_failure

    monkeypatch.setattr(
        audit_bootstrap_module.JournalConversationProjector,
        "bootstrap_thread",
        _fail_projection,
    )
    pool = _pool(tmp_path, core, events, store=store)

    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(session_id="ses-proj-fail", entry_skill_id="entry")

    assert getattr(caught.value, "code", None) == "audit_engine_creation_failed"
    assert caught.value.__cause__ is projection_failure
    assert "secret" not in str(caught.value)
    result = caught.value.finish_result
    assert result.audit_complete is expected_audit_complete
    assert result.lease_released is True
    assert events == [
        "journal_create",
        "projection",
        "journal_terminal",
        "lease_close",
    ]
    assert core.close_calls == 1
    assert core.global_close_calls == 0
    assert len(core.appended) == 1
    assert [record.record_type for record in core.appended[0]] == [
        "thread_terminal",
        "session_ended",
    ]
    assert core.appended[0][0].payload["status"] == "error"
    assert core.appended[0][0].payload["end_reason"] == "projection_bootstrap_failed"
    assert (
        core.appended[0][0].payload["stable_error"]["code"]
        == "projection_bootstrap_failed"
    )
    assert "ses-proj-fail" not in pool._engines  # noqa: SLF001
    assert "ses-proj-fail" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
async def test_legacy_resume_rejects_audited_marker_before_history_load(
    tmp_path: Path,
) -> None:
    """legacy resume 在 load_thread/Engine/actor 前稳定拒绝完整 audited marker。"""
    events: list[str] = []
    store = _SpyStore(tmp_path / "threads", events)
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id="thr-audited",
        cwd="/cwd",
        entry_skill_id="entry",
        source="session:audited",
        extra={
            "audit_required": True,
            "journal_session_id": "ses-audited",
            "journal_schema_version": 1,
        },
    )
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=SimClient(turns=[]),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
    )

    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(
            session_id="legacy",
            entry_skill_id="entry",
            resume_thread_id="thr-audited",
        )

    assert getattr(caught.value, "code", None) == "audit_downgrade_forbidden"
    assert caught.value.thread_id == "thr-audited"
    assert store.load_calls == 0
    assert "legacy" not in pool._engines  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
async def test_self_contained_marker_blocks_resume_when_directory_metadata_missing(
    tmp_path: Path,
) -> None:
    """directory 行缺失时仍从 JSONL 首行 marker 阻止 downgrade 绕过。"""
    events: list[str] = []
    store = _SpyStore(tmp_path / "threads", events)
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id="thr-file-marker",
        cwd=None,
        entry_skill_id="entry",
        source="session:audited",
        extra={
            "audit_required": True,
            "journal_session_id": "ses-file-marker",
            "journal_schema_version": 1,
        },
    )

    @dataclass
    class _MissingDirectory:
        """模拟派生 index 行被删除。"""

        async def get_metadata(self, thread_id: str) -> None:
            del thread_id
            return

        async def close(self) -> None:
            return None

    await store._directory.close()  # noqa: SLF001
    store._directory = _MissingDirectory()  # type: ignore[assignment]  # noqa: SLF001
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=SimClient(turns=[]),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
    )

    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(
            session_id="legacy-file",
            entry_skill_id="entry",
            resume_thread_id="thr-file-marker",
        )

    assert getattr(caught.value, "code", None) == "audit_downgrade_forbidden"
    assert store.load_calls == 0
    await pool.close()


@pytest.mark.asyncio
async def test_cached_legacy_engine_does_not_mask_audited_resume_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache 命中仍忽略普通 resume 参数，但 audited marker 必须 fail-closed。"""
    events: list[str] = []
    store = _SpyStore(tmp_path / "threads", events)
    projector = JournalConversationProjector(store)
    await projector.bootstrap_thread(
        thread_id="thr-audited-cache",
        cwd=None,
        entry_skill_id="entry",
        source="session:audited",
        extra={
            "audit_required": True,
            "journal_session_id": "ses-audited-cache",
            "journal_schema_version": 1,
        },
    )
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    pool = EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=SimClient(turns=[]),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
    )
    cached = await pool.get_or_create(session_id="cached", entry_skill_id="entry")

    assert (
        await pool.get_or_create(
            session_id="cached",
            entry_skill_id="entry",
            resume_thread_id="ordinary-missing-thread",
        )
        is cached
    )
    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(
            session_id="cached",
            entry_skill_id="entry",
            resume_thread_id="thr-audited-cache",
        )
    assert getattr(caught.value, "code", None) == "audit_downgrade_forbidden"
    assert store.load_calls == 0
    await pool.close()
