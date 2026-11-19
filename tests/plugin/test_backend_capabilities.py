"""插件提供存储后端（plugin-backend-providers，两段加载第一段）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import AuditConfig
from taifeng.plugin import PluginCapabilityError, PluginContext, PluginSetupError, PluginSpec
from taifeng.plugin.backends import PluginBackends, collect_backend_providers
from tests.conftest import run_until_root_done
from tests.loop.test_audit_attachments import _CAPS
from tests.loop.test_pool_external_store import _MemoryStore, _skills

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _Provider:
    """可选实现 provide_backends 的插件。"""

    def __init__(
        self,
        plugin_id: str = "store",
        *,
        declare: Callable[[PluginBackends], None] | None = None,
        fail_setup: bool = False,
    ) -> None:
        self.spec = PluginSpec(id=plugin_id, version="1")
        self._declare = declare
        self._fail_setup = fail_setup
        self.store_closed_at_close: int | None = None
        self.store: _MemoryStore | None = None

    def provide_backends(self, backends: PluginBackends) -> None:
        if self._declare is not None:
            self._declare(backends)

    async def setup(self, ctx: PluginContext) -> None:
        if self._fail_setup:
            raise RuntimeError("boom")

    async def close(self) -> None:
        if self.store is not None:
            self.store_closed_at_close = self.store.closed


def _store_plugin(store: _MemoryStore, **kwargs: Any) -> _Provider:
    plugin = _Provider(declare=lambda b: b.message_store(lambda: store), **kwargs)
    plugin.store = store
    return plugin


async def _create(tmp_path: Path, **kwargs: Any) -> taifeng.EnginePool:
    kwargs.setdefault("model_client", SimClient(turns=[SimTurn(text="ok")]))
    return await taifeng.EnginePool.create(skills_dir=_skills(tmp_path), compressors=[], **kwargs)


# ---------------------------------------------------------------------------
# 声明阶段
# ---------------------------------------------------------------------------


def test_two_plugins_providing_same_backend_conflict() -> None:
    a = _Provider("a", declare=lambda b: b.message_store(_MemoryStore))
    b = _Provider("b", declare=lambda b: b.message_store(_MemoryStore))
    with pytest.raises(PluginCapabilityError) as caught:
        collect_backend_providers([a, b])
    assert caught.value.capability == "message_store"


def test_declaring_after_provide_returns_is_rejected() -> None:
    kept: list[PluginBackends] = []
    collect_backend_providers([_Provider(declare=kept.append)])
    with pytest.raises(PluginCapabilityError):
        kept[0].message_store(_MemoryStore)


def test_plugins_without_provide_backends_are_skipped() -> None:
    class Plain:
        spec = PluginSpec(id="plain", version="1")

        async def setup(self, ctx: PluginContext) -> None:
            return None

    assert collect_backend_providers([Plain()]) == {}


# ---------------------------------------------------------------------------
# pool 接线
# ---------------------------------------------------------------------------


async def test_plugin_store_is_used_and_closed_after_plugin(tmp_path: Path) -> None:
    store = _MemoryStore()
    plugin = _store_plugin(store)
    pool = await _create(tmp_path, plugins=(plugin,))
    engine = await pool.get_or_create(session_id="s", entry_skill_id="entry")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="hi"))
    assert events[-1].msg.kind == "turn_completed"
    assert store.threads  # 会话落在插件提供的 store 里
    await pool.close()
    assert plugin.store_closed_at_close == 0  # 插件关闭时 store 仍可用
    assert store.closed == 1


async def test_resume_with_same_provider_plugin(tmp_path: Path) -> None:
    store = _MemoryStore()
    first = await _create(tmp_path, plugins=(_store_plugin(store),))
    engine = await first.get_or_create(session_id="s", entry_skill_id="entry")
    await run_until_root_done(engine, taifeng.UserMessage(text="one"))
    thread_id = engine.thread_id
    await first.close()

    second = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", compressors=[], plugins=(_store_plugin(store),),
        model_client=SimClient(turns=[SimTurn(text="two")]),
    )
    resumed = await second.get_or_create(
        session_id="s", entry_skill_id="entry", resume_thread_id=thread_id,
    )
    assert [i.kind for i in resumed.history_snapshot()][:2] == ["user_message", "assistant_message"]
    await second.close()


@pytest.mark.parametrize(
    "explicit",
    [
        lambda tmp: {"message_store": _MemoryStore()},
        lambda tmp: {"threads_dir": tmp / "threads"},
    ],
    ids=["message_store", "threads_dir"],
)
async def test_conflict_with_host_arguments(tmp_path: Path, explicit: Any) -> None:
    store = _MemoryStore()
    with pytest.raises(PluginCapabilityError):
        await _create(tmp_path, plugins=(_store_plugin(store),), **explicit(tmp_path))
    assert store.closed == 0  # 冲突在构造任何后端之前被拒


async def test_thread_directory_conflict_with_host(tmp_path: Path) -> None:
    plugin = _Provider(declare=lambda b: b.thread_directory(taifeng.NullThreadDirectory))
    with pytest.raises(PluginCapabilityError):
        await _create(
            tmp_path, threads_dir=tmp_path / "threads", plugins=(plugin,),
            thread_directory=taifeng.NullThreadDirectory(),
        )


async def test_factory_failure_is_attributed_to_plugin(tmp_path: Path) -> None:
    def broken() -> _MemoryStore:
        raise OSError("db down")

    plugin = _Provider("db", declare=lambda b: b.message_store(broken))
    with pytest.raises(PluginSetupError) as caught:
        await _create(tmp_path, plugins=(plugin,))
    assert caught.value.plugin_id == "db"
    assert isinstance(caught.value.__cause__, OSError)


async def test_invalid_plugin_store_is_closed(tmp_path: Path) -> None:
    class NotAStore:
        closed = 0

        async def close(self) -> None:
            NotAStore.closed += 1

    plugin = _Provider(declare=lambda b: b.message_store(NotAStore))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await _create(tmp_path, plugins=(plugin,))
    assert NotAStore.closed == 1


async def test_later_setup_failure_closes_plugin_store(tmp_path: Path) -> None:
    store = _MemoryStore()
    with pytest.raises(PluginSetupError):
        await _create(tmp_path, plugins=(_store_plugin(store, fail_setup=True),))
    assert store.closed == 1


async def test_plugin_provided_audit_config(tmp_path: Path) -> None:
    """插件提供整份 AuditConfig：pool 进入审计模式，插件不能再注册工具（静态门已封）。"""
    denied: list[str] = []

    class AuditPlugin:
        spec = PluginSpec(id="audit", version="1")

        def provide_backends(self, backends: PluginBackends) -> None:
            backends.audit(lambda: AuditConfig(
                journal_core=JsonlSessionJournalCore(tmp_path / "journal"),
                writer_id="writer-plugin", max_attachment_bytes=65536,
                max_total_attachment_bytes=1048576,
            ))

        async def setup(self, ctx: PluginContext) -> None:
            try:
                ctx.hooks.register("pre_turn", _noop_hook)
            except PluginCapabilityError as exc:
                denied.append(exc.capability)

    sim = SimClient(turns=[SimTurn(text="ok")], capabilities=_CAPS)
    pool = await _create(
        tmp_path, threads_dir=tmp_path / "threads", plugins=(AuditPlugin(),),
        model_client=AttemptObservableClientAdapter(sim, provider="sim", default_model="sim-model"),
    )
    assert pool._audit is not None  # noqa: SLF001
    assert pool._audit.writer_id == "writer-plugin"  # noqa: SLF001
    assert denied == ["hooks"]
    await pool.close()


async def _noop_hook(hook: Any, ctx: Any) -> Any:
    from taifeng.hooks.types import HookDecision

    return HookDecision.ok()


# ---------------------------------------------------------------------------
# 附件存储（attachment-store，ADR 0148）
# ---------------------------------------------------------------------------


async def test_plugin_provides_attachment_store(tmp_path: Path) -> None:
    from taifeng.conversation.attachment_store import LocalAttachmentStore

    store = LocalAttachmentStore(tmp_path / "blobs")
    plugin = _Provider(declare=lambda b: b.attachment_store(lambda: store))
    pool = await _create(tmp_path, storage_dir=tmp_path / "threads", plugins=[plugin])
    assert pool.attachments is store
    await pool.close()


async def test_plugin_and_host_attachment_store_conflict(tmp_path: Path) -> None:
    from taifeng.conversation.attachment_store import LocalAttachmentStore

    plugin = _Provider(declare=lambda b: b.attachment_store(lambda: LocalAttachmentStore(tmp_path / "a")))
    with pytest.raises(PluginCapabilityError):
        await _create(
            tmp_path, storage_dir=tmp_path / "threads", plugins=[plugin],
            attachment_store=LocalAttachmentStore(tmp_path / "b"),
        )


async def test_plugin_attachment_store_must_implement_protocol(tmp_path: Path) -> None:
    plugin = _Provider(declare=lambda b: b.attachment_store(lambda: object()))  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="AttachmentStore"):
        await _create(tmp_path, storage_dir=tmp_path / "threads", plugins=[plugin])
