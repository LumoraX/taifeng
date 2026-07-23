"""Task 5.3–5.5 规格复审回归测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng.loop.audit_bootstrap as audit_bootstrap_module
import taifeng.loop.pool as pool_module
from taifeng.conversation.journal.materialization import _TARGETS
from taifeng.conversation.journal.models import (
    JournalAck,
    SessionCreateResult,
    SessionLease,
)
from taifeng.conversation.journal.projector import JournalConversationProjector
from taifeng.conversation.models import ResponseItem, ThreadInfo
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient
from taifeng.loop.audit_config import AuditCapabilityError
from taifeng.loop.pool import EnginePool
from taifeng.tool.registry import ToolRegistry
from tests.loop.test_audit_engine_bootstrap import (
    _config,
    _EngineSpy,
    _JournalCore,
    _ObservedClient,
    _pool,
    _Registry,
    _SpyStore,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


class _ResultSubclass(SessionCreateResult):
    """伪造非 exact create result。"""


class _LeaseSubclass(SessionLease):
    """伪造非 exact lease。"""


class _AckSubclass(JournalAck):
    """伪造非 exact ack。"""


class _MalformedCore(_JournalCore):
    """把可信 create result 转换为指定 malformed 结果。"""

    def __init__(
        self,
        events: list[str],
        transform: Callable[[SessionCreateResult], object],
    ) -> None:
        super().__init__(events)
        self._transform = transform

    async def create_session(self, descriptor: Any) -> Any:
        """先取可信 result，再注入协议违约。"""
        result = await super().create_session(descriptor)
        return self._transform(result)


def _replace_lease(
    result: SessionCreateResult,
    **updates: object,
) -> SessionCreateResult:
    """替换 result lease。"""
    return result.model_copy(update={"lease": result.lease.model_copy(update=updates)})


def _replace_ack(
    result: SessionCreateResult,
    **updates: object,
) -> SessionCreateResult:
    """替换 result ack。"""
    return result.model_copy(update={"ack": result.ack.model_copy(update=updates)})


def _tamper_ack(
    result: SessionCreateResult,
    field: str,
    value: object,
) -> SessionCreateResult:
    """绕过 frozen DTO 模拟 caller tamper。"""
    object.__setattr__(result.ack, field, value)
    return result


def _tamper_result_ack(result: SessionCreateResult) -> SessionCreateResult:
    """把 exact result 的 ack 偷换为任意对象。"""
    object.__setattr__(result, "ack", object())
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transform", "expected_close"),
    [
        (lambda result: object(), 0),
        (
            lambda result: _ResultSubclass(
                lease=result.lease,
                ack=result.ack,
            ),
            0,
        ),
        (
            lambda result: result.model_copy(
                update={
                    "lease": _LeaseSubclass.model_validate(
                        result.lease.model_dump(mode="python")
                    )
                }
            ),
            0,
        ),
        (lambda result: _replace_lease(result, session_id="other"), 0),
        (lambda result: _replace_lease(result, writer_id="other"), 0),
        (lambda result: _replace_lease(result, writer_epoch=2), 0),
        (
            lambda result: result.model_copy(
                update={
                    "ack": _AckSubclass.model_validate(
                        result.ack.model_dump(mode="python")
                    )
                }
            ),
            1,
        ),
        (lambda result: _replace_ack(result, session_id="other"), 1),
        (lambda result: _replace_ack(result, writer_epoch=2), 1),
        (lambda result: _tamper_ack(result, "durability", "unknown"), 1),
        (lambda result: _replace_ack(result, first_seq=2), 1),
        (lambda result: _replace_ack(result, last_seq=4), 1),
        (
            lambda result: _replace_ack(
                result,
                record_ids=(
                    result.ack.record_ids[1],
                    result.ack.record_ids[0],
                    result.ack.record_ids[2],
                ),
            ),
            1,
        ),
        (
            lambda result: _replace_ack(
                result,
                record_ids=(result.ack.record_ids[0],) * 3,
            ),
            1,
        ),
        (
            lambda result: _replace_ack(
                result,
                record_ids=result.ack.record_ids[:2],
            ),
            1,
        ),
        (lambda result: _tamper_ack(result, "tail_hash", "bad"), 1),
        (lambda result: _tamper_ack(result, "tail_hash", 7), 1),
        (_tamper_result_ack, 1),
    ],
)
async def test_malformed_create_result_is_rejected_before_projection_or_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transform: Callable[[SessionCreateResult], object],
    expected_close: int,
) -> None:
    """只对可信 lease + 不可信 ack 做一次 emergency close。"""
    events: list[str] = []
    core = _MalformedCore(events, transform)
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: pytest.fail(f"engine constructed: {kwargs}"),
    )
    pool = _pool(tmp_path, core, events)

    with pytest.raises(RuntimeError) as caught:
        await pool.get_or_create(session_id="ses-malformed", entry_skill_id="entry")

    assert getattr(caught.value, "code", None) == "audit_engine_creation_failed"
    assert core.close_calls == expected_close
    assert core.appended == []
    assert events == [
        "journal_create",
        *(["lease_close"] if expected_close else []),
    ]
    assert pool._engines == {}  # noqa: SLF001
    assert pool._engine_tasks == {}  # noqa: SLF001
    assert pool._audit_sessions == {}  # noqa: SLF001
    await pool.close()
    assert core.global_close_calls == 0


@pytest.mark.asyncio
async def test_audited_cache_hit_resume_is_rejected_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cached audited Engine 也必须执行独立 resume gate。"""
    events: list[str] = []
    core = _JournalCore(events)
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    pool = _pool(tmp_path, core, events)
    cached = await pool.get_or_create(session_id="ses-cache", entry_skill_id="entry")

    with pytest.raises(AuditCapabilityError) as caught:
        await pool.get_or_create(
            session_id="ses-cache",
            entry_skill_id="entry",
            resume_thread_id="any-resume",
        )

    assert caught.value.code == "audit_resume_unsupported"
    assert pool._engines["ses-cache"] is cached  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
async def test_audited_release_finishes_once_after_actor_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """normal release 写 root/session terminal，单次 close 并清 ownership。"""
    events: list[str] = []
    core = _JournalCore(events)
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(session_id="ses-release", entry_skill_id="entry")

    await asyncio.gather(
        pool.release("ses-release"),
        pool.release("ses-release"),
    )

    assert events.count("engine_shutdown") == 1
    assert events.index("engine_shutdown") < events.index("journal_terminal")
    assert [record.record_type for record in core.appended[0]] == [
        "thread_terminal",
        "session_ended",
    ]
    assert core.close_calls == 1
    assert core.global_close_calls == 0
    assert "ses-release" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()
    assert core.close_calls == 1


@pytest.mark.asyncio
async def test_audited_release_failure_is_stable_and_clears_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish incomplete 暴露真实 result，不保留失效 ownership entry。"""
    events: list[str] = []
    core = _JournalCore(events, fail_terminal=OSError("secret=terminal"))
    monkeypatch.setattr(
        pool_module,
        "AgentEngine",
        lambda **kwargs: _EngineSpy(events=events, **kwargs),
    )
    pool = _pool(tmp_path, core, events)
    await pool.get_or_create(session_id="ses-release-fail", entry_skill_id="entry")

    with pytest.raises(RuntimeError) as caught:
        await pool.release("ses-release-fail")

    assert getattr(caught.value, "code", None) == "audit_session_release_incomplete"
    assert caught.value.finish_result.audit_complete is False
    assert caught.value.finish_result.lease_released is True
    assert "secret" not in str(caught.value)
    assert core.close_calls == 1
    assert "ses-release-fail" not in pool._audit_sessions  # noqa: SLF001
    await pool.close()


class _MaliciousStore:
    """同名 property 必须在 nominal binding 外完全不执行。"""

    def __init__(self, projection: JsonlMessageStore | None = None) -> None:
        self.reads = 0
        self._projection = projection

    @property
    def audit_projection_store(self) -> JsonlMessageStore | None:
        """若 generic getattr 执行，记录 spoof。"""
        self.reads += 1
        return self._projection

    @property
    def audit_custom_directory(self) -> None:
        """另一个不得执行的 audit-only descriptor。"""
        self.reads += 1
        return None

    async def create_thread(self, **kwargs: object) -> str:
        del kwargs
        return "legacy"

    async def append(self, item: ResponseItem) -> None:
        del item

    async def append_batch(self, items: list[ResponseItem]) -> None:
        del items

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        del thread_id

        async def _items() -> AsyncIterator[ResponseItem]:
            if False:
                yield ResponseItem.model_construct()

        return _items()

    async def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 50,
    ) -> list[ThreadInfo]:
        del cwd, limit
        return []

    async def select_resume_path(self, cwd: str) -> str | None:
        del cwd
        return None

    async def close(self) -> None:
        return None


def _direct_pool(
    store: Any,
    *,
    core: _JournalCore | None,
) -> EnginePool:
    """构造 store binding 测试 pool。"""
    return EnginePool(
        skill_registry=_Registry(),  # type: ignore[arg-type]
        model_client=_ObservedClient() if core is not None else SimClient(turns=[]),
        store=store,
        tool_registry=ToolRegistry(),
        compressors=[],
        audit=_config(core) if core is not None else None,
    )


def test_legacy_custom_store_does_not_touch_audit_descriptors() -> None:
    """audit=None 在任何 audit-only descriptor 读取前返回。"""
    store = _MaliciousStore()

    _direct_pool(store, core=None)

    assert store.reads == 0


@pytest.mark.parametrize("kind", ["subclass", "wrapper-spoof"])
def test_strict_audit_rejects_non_exact_store_without_descriptor_execution(
    tmp_path: Path,
    kind: str,
) -> None:
    """subclass/custom wrapper 不能伪装成默认 projection store。"""
    events: list[str] = []
    core = _JournalCore(events)
    exact = JsonlMessageStore(tmp_path / "exact")
    store: object
    if kind == "subclass":
        store = _SpyStore(tmp_path / "subclass", events)
    else:
        store = _MaliciousStore(exact)

    with pytest.raises(AuditCapabilityError) as caught:
        _direct_pool(store, core=core)

    assert caught.value.code == "audit_custom_store_unsupported"
    if isinstance(store, _MaliciousStore):
        assert store.reads == 0


@pytest.mark.asyncio
async def test_public_create_static_failure_releases_projection_handle(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """factory static gate 失败必须关闭已创建的默认 projection handle。"""
    root = (tmp_path / "threads").resolve()
    core = _JournalCore([])

    with pytest.raises(AuditCapabilityError):
        await EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root,
            model_client=_ObservedClient(),
            compressors=[],
            audit=_config(core),
        )

    assert root not in _TARGETS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "expected_reason"),
    [
        ("projection-id", "projection_bootstrap_failed"),
        ("engine", "engine_bootstrap_failed"),
    ],
)
async def test_bootstrap_failure_uses_stage_specific_terminal_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_reason: str,
) -> None:
    """projection/id 与 Engine 构造失败使用不同稳定 reason。"""
    events: list[str] = []
    core = _JournalCore(events)
    if failure_point == "projection-id":
        original = JournalConversationProjector.bootstrap_thread

        async def _wrong_id(
            projector: JournalConversationProjector,
            **kwargs: Any,
        ) -> str:
            await original(projector, **kwargs)
            return "thr-wrong"

        monkeypatch.setattr(
            audit_bootstrap_module.JournalConversationProjector,
            "bootstrap_thread",
            _wrong_id,
        )
        monkeypatch.setattr(
            pool_module,
            "AgentEngine",
            lambda **kwargs: pytest.fail(f"engine constructed: {kwargs}"),
        )
    else:
        monkeypatch.setattr(
            pool_module,
            "AgentEngine",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("engine secret")),
        )
    pool = _pool(tmp_path, core, events)

    with pytest.raises(RuntimeError):
        await pool.get_or_create(
            session_id=f"ses-{failure_point}",
            entry_skill_id="entry",
        )

    terminal = core.appended[0][0]
    assert terminal.record_type == "thread_terminal"
    assert terminal.payload["end_reason"] == expected_reason
    assert terminal.payload["stable_error"]["code"] == expected_reason
