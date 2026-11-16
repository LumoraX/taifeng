"""``EnginePool.create(plugins=...)`` 接线测试（plugin-core 任务 3）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.providers.sim import SimClient
from taifeng.loop.pool import EnginePool
from taifeng.plugin import PluginContext, PluginSetupError, PluginSpec

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.event import EventMsg


class _CollectSink:
    """收集 pool 级事件的 sink。"""

    def __init__(self) -> None:
        self.events: list[EventMsg] = []

    async def handle(self, ev: EventMsg) -> None:
        self.events.append(ev)

    def kinds(self) -> list[str]:
        return [ev.msg.kind for ev in self.events if ev.msg.kind.startswith("plugin_")]


class _Handle:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def aclose(self) -> None:
        self._log.append("revoke")


class _Recording:
    """记录生命周期，并在 close 时快照 pool 的活跃会话与令牌状态。"""

    def __init__(self, plugin_id: str = "rec", *, fail: bool = False) -> None:
        self.spec = PluginSpec(id=plugin_id, version="1")
        self.log: list[str] = []
        self.pool: EnginePool | None = None
        self.sessions_at_close: dict[str, object] | None = None
        self.token: CancellationToken | None = None
        self.token_cancelled_at_close: bool | None = None
        self._fail = fail

    async def setup(self, ctx: PluginContext) -> None:
        self.log.append("setup")
        self.token = ctx.cancel
        ctx._track(_Handle(self.log))  # noqa: SLF001
        if self._fail:
            raise RuntimeError("boom")

    async def close(self) -> None:
        self.log.append("close")
        assert self.token is not None
        self.token_cancelled_at_close = self.token.is_cancelled
        if self.pool is not None:
            self.sessions_at_close = self.pool.introspect()


async def _pool(tmp_path: Path, skills_dir: Path, **kwargs: object) -> EnginePool:
    return await EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[]),
        compressors=[],
        **kwargs,  # type: ignore[arg-type]
    )


async def test_plugins_loaded_before_create_returns(tmp_path: Path, skills_dir: Path) -> None:
    plugin = _Recording()
    pool = await _pool(tmp_path, skills_dir, plugins=(plugin,))
    assert plugin.log == ["setup"]
    assert pool._plugin_manager is not None  # noqa: SLF001
    assert pool._plugin_manager.snapshot().loaded_ids == ("rec",)  # noqa: SLF001
    assert pool.introspect() == {}  # 加载时还没有任何 engine
    await pool.close()
    assert plugin.log == ["setup", "close", "revoke"]


async def test_plugins_close_after_sessions_released_before_cancel(
    tmp_path: Path, skills_dir: Path,
) -> None:
    plugin = _Recording()
    pool = await _pool(tmp_path, skills_dir, plugins=(plugin,))
    plugin.pool = pool
    await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    assert "s1" in pool.introspect()
    await pool.close()
    assert plugin.sessions_at_close == {}
    # 关闭插件时根令牌尚未取消，插件收尾仍可正常 await；pool 关闭后才被取消
    assert plugin.token_cancelled_at_close is False
    assert plugin.token is not None and plugin.token.is_cancelled
    assert plugin.token.name == "pool/plugin:rec"


async def test_lifecycle_events_reach_pool_sink(tmp_path: Path, skills_dir: Path) -> None:
    sink = _CollectSink()
    pool = await _pool(tmp_path, skills_dir, plugins=(_Recording(),), sink=sink)
    await pool.close()
    assert sink.kinds() == ["plugin_loaded", "plugin_unloaded"]
    loaded = sink.events[[e.msg.kind for e in sink.events].index("plugin_loaded")]
    assert loaded.submission_id == "*"
    assert loaded.seq == 0
    assert loaded.msg.data["plugin_id"] == "rec"


async def test_default_path_unchanged_without_plugins(tmp_path: Path, skills_dir: Path) -> None:
    sink = _CollectSink()
    pool = await _pool(tmp_path, skills_dir, sink=sink)
    baseline = await _pool(tmp_path / "b", skills_dir, plugins=())
    assert pool._plugin_manager is None  # noqa: SLF001
    assert baseline._plugin_manager is None  # noqa: SLF001
    assert pool.tool_registry.version == baseline.tool_registry.version
    await pool.close()
    await baseline.close()
    assert sink.kinds() == []


async def test_setup_failure_fails_create_and_releases_resources(
    tmp_path: Path, skills_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    original_close = JsonlMessageStore.close

    async def _spy_close(self: JsonlMessageStore) -> None:
        closed.append("store")
        await original_close(self)

    monkeypatch.setattr(JsonlMessageStore, "close", _spy_close)
    sink = _CollectSink()
    good = _Recording("good")
    bad = _Recording("bad", fail=True)
    with pytest.raises(PluginSetupError) as caught:
        await _pool(tmp_path, skills_dir, plugins=(good, bad), sink=sink)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert good.log == ["setup", "close", "revoke"]
    assert bad.log == ["setup", "revoke"]
    assert closed == ["store"]
    assert sink.kinds() == ["plugin_loaded", "plugin_unloaded", "plugin_failed"]


async def test_pool_close_is_idempotent_with_plugins(tmp_path: Path, skills_dir: Path) -> None:
    plugin = _Recording()
    pool = await _pool(tmp_path, skills_dir, plugins=(plugin,))
    await pool.close()
    await pool.close()
    assert plugin.log.count("close") == 1
