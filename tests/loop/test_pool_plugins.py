"""``EnginePool.create(plugins=...)`` 接线测试（plugin-core 任务 3）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


# ---------------------------------------------------------------------------
# plugin-tool-hook-registration：插件工具 / 钩子在真实 turn 中生效
# ---------------------------------------------------------------------------


def _agent_skill(root: Path) -> Path:
    skills = root / "skills"
    (skills / "agent").mkdir(parents=True)
    (skills / "agent" / "SKILL.md").write_text(
        "---\nname: agent\ndescription: d\ntype: composite\nentry: true\n"
        "tool_names: [plugin_echo]\n---\n# agent\n",
        encoding="utf-8",
    )
    return skills


def _echo_spec() -> Any:
    from taifeng.tool.spec import ToolResult, ToolSpec

    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult.ok("ECHO-FROM-PLUGIN")

    return ToolSpec(
        name="plugin_echo", description="echo", input_schema={"type": "object", "properties": {}},
        handler=handler,
    )


class _ToolAndHookPlugin:
    """注册一个工具与一个 post_tool_use 钩子。"""

    spec = PluginSpec(id="echo", version="1")

    def __init__(self) -> None:
        self.hook_calls: list[str] = []

    async def setup(self, ctx: PluginContext) -> None:
        from taifeng.hooks.types import HookDecision

        async def on_post_tool(hook: Any, hook_ctx: Any) -> HookDecision:
            self.hook_calls.append(hook.tool_name)
            return HookDecision.ok()

        ctx.tools.register(_echo_spec())
        ctx.hooks.register("post_tool_use", on_post_tool)


def _echo_turns() -> list[Any]:
    from taifeng.llm.providers.sim import SimTurn

    return [
        SimTurn(text="", tool_calls=[{"id": "c1", "name": "plugin_echo", "arguments": "{}"}]),
        SimTurn(text="done"),
    ]


async def _run_turn(engine: Any, text: str) -> list[Any]:
    import taifeng
    from tests.conftest import run_until_root_done

    return await run_until_root_done(engine, taifeng.UserMessage(text=text))


async def test_plugin_tool_and_hook_used_in_a_real_turn(tmp_path: Path) -> None:
    plugin = _ToolAndHookPlugin()
    pool = await EnginePool.create(
        skills_dir=_agent_skill(tmp_path), threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=_echo_turns()), compressors=[], plugins=(plugin,),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="agent")
    events = await _run_turn(engine, "go")
    completed = [e for e in events if e.msg.kind == "tool_call_completed"]
    assert completed and completed[0].msg.data.get("is_error") is not True
    assert plugin.hook_calls == ["plugin_echo"]
    await pool.close()
    assert pool.tool_registry.get("plugin_echo") is None
    assert pool._hooks.registry.handlers("post_tool_use") == []  # noqa: SLF001


async def test_hooks_stay_none_when_no_plugin_registers_hooks(tmp_path: Path, skills_dir: Path) -> None:
    pool = await _pool(tmp_path, skills_dir, plugins=(_Recording(),))
    assert pool._hooks is None  # noqa: SLF001
    await pool.close()


async def test_plugin_tool_conflicting_with_extra_tools_fails_create(
    tmp_path: Path, skills_dir: Path,
) -> None:
    with pytest.raises(PluginSetupError) as caught:
        await _pool(tmp_path, skills_dir, extra_tools=[_echo_spec()], plugins=(_ToolAndHookPlugin(),))
    from taifeng.plugin import PluginCapabilityError

    assert isinstance(caught.value.__cause__, PluginCapabilityError)


async def test_resume_without_plugin_stops_offering_its_tool(tmp_path: Path) -> None:
    """R5：插件缺席时恢复会话——历史里的插件工具调用照常重放，该工具不再提供给模型。

    模型若仍幻觉调用它，走既有 ``not_offered`` 拒绝路径（is_error 结果、turn 不中断），
    见 tests/loop/test_tool_whitelist 一族；sim 合同禁止脚本调用未提供的工具，故此处只断言「不提供」。
    """
    from taifeng.llm.providers.sim import SimTurn

    skills = _agent_skill(tmp_path)
    first = await EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=_echo_turns()), compressors=[], plugins=(_ToolAndHookPlugin(),),
    )
    engine = await first.get_or_create(session_id="s", entry_skill_id="agent")
    await _run_turn(engine, "go")
    thread_id = engine.thread_id
    await first.close()

    client = SimClient(turns=[SimTurn(text="resumed")])
    second = await EnginePool.create(
        skills_dir=skills, threads_dir=tmp_path / "threads", model_client=client, compressors=[],
    )
    resumed = await second.get_or_create(
        session_id="s", entry_skill_id="agent", resume_thread_id=thread_id,
    )
    events = await _run_turn(resumed, "again")
    assert events[-1].msg.kind == "turn_completed"
    request = client.ledger.last_request()
    assert request is not None
    assert "plugin_echo" not in {t.name for t in request.request.tools}
    assert client.ledger.saw_function_call("c1")  # 历史里的插件调用照常重放
    await second.close()


# ---------------------------------------------------------------------------
# skill-dir-registration：插件挂上的 skill 目录
# ---------------------------------------------------------------------------


def _plugin_skills(root: Path) -> Path:
    # 每个 skill 目录自成一体：child_skills 只能引用同目录内的 skill（loader 按目录校验）
    skills = root / "plugin-skills"
    (skills / "plugin-entry").mkdir(parents=True)
    (skills / "plugin-entry" / "SKILL.md").write_text(
        "---\nname: plugin-entry\ndescription: d\ntype: composite\nentry: true\n"
        "child_skills: [plugin-leaf]\n---\n# plugin entry\n",
        encoding="utf-8",
    )
    (skills / "plugin-leaf").mkdir()
    (skills / "plugin-leaf" / "SKILL.md").write_text(
        "---\nname: plugin-leaf\ndescription: d\ntype: atomic\n---\n# leaf\n", encoding="utf-8",
    )
    return skills


class _SkillsPlugin:
    spec = PluginSpec(id="skills", version="1")

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.handle: Any = None

    async def setup(self, ctx: PluginContext) -> None:
        self.handle = await ctx.skills.add_dir(self.directory, source="marketplace")


async def test_plugin_skill_dir_usable_then_removed(tmp_path: Path, skills_dir: Path) -> None:
    from taifeng.llm.providers.sim import SimTurn

    plugin = _SkillsPlugin(_plugin_skills(tmp_path))
    pool = await EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[SimTurn(text="hi")]), compressors=[], plugins=(plugin,),
    )
    skill = pool.skill_registry.get("plugin-entry")
    assert skill is not None and skill.source == "marketplace"
    engine = await pool.get_or_create(session_id="s", entry_skill_id="plugin-entry")
    events = await _run_turn(engine, "go")
    assert events[-1].msg.kind == "turn_completed"
    await pool.close()
    assert pool.skill_registry.get("plugin-entry") is None


async def test_removal_refused_while_entry_skill_in_use(tmp_path: Path, skills_dir: Path) -> None:
    from taifeng.plugin import PluginCapabilityError

    plugin = _SkillsPlugin(_plugin_skills(tmp_path))
    pool = await _pool(tmp_path, skills_dir, plugins=(plugin,))
    await pool.get_or_create(session_id="s", entry_skill_id="plugin-entry")
    with pytest.raises(PluginCapabilityError):
        await plugin.handle.aclose()
    assert pool.skill_registry.get("plugin-entry") is not None
    await pool.close()  # 关闭时会话已释放，撤销成功
    assert pool.skill_registry.get("plugin-entry") is None


async def test_plugin_skill_colliding_with_host_skill_fails_create(
    tmp_path: Path, skills_dir: Path,
) -> None:
    from taifeng.plugin import PluginCapabilityError

    clash = tmp_path / "clash"
    (clash / "style-checker").mkdir(parents=True)
    (clash / "style-checker" / "SKILL.md").write_text(
        "---\nname: style-checker\ndescription: d\ntype: atomic\n---\n# x\n", encoding="utf-8",
    )
    with pytest.raises(PluginSetupError) as caught:
        await _pool(tmp_path, skills_dir, plugins=(_SkillsPlugin(clash),))
    assert isinstance(caught.value.__cause__, PluginCapabilityError)


async def test_resume_with_entry_skill_from_absent_plugin_is_rejected(
    tmp_path: Path, skills_dir: Path,
) -> None:
    """R5 现状：入口 skill 来自缺席插件目录时，恢复会话以显式错误失败，不静默换入口。"""
    from taifeng.llm.providers.sim import SimTurn

    first = await EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[SimTurn(text="hi")]), compressors=[],
        plugins=(_SkillsPlugin(_plugin_skills(tmp_path)),),
    )
    engine = await first.get_or_create(session_id="s", entry_skill_id="plugin-entry")
    await _run_turn(engine, "go")
    thread_id = engine.thread_id
    await first.close()

    second = await _pool(tmp_path, skills_dir)
    with pytest.raises(ValueError, match="unknown skill: plugin-entry"):
        await second.get_or_create(
            session_id="s", entry_skill_id="plugin-entry", resume_thread_id=thread_id,
        )
    await second.close()


# ---------------------------------------------------------------------------
# telemetry-fanout-sink：插件 sink
# ---------------------------------------------------------------------------


class _SinkPlugin:
    def __init__(self, plugin_id: str, requires: tuple[str, ...] = ()) -> None:
        self.spec = PluginSpec(id=plugin_id, version="1", requires=requires)
        self.sink = _CollectSink()

    async def setup(self, ctx: PluginContext) -> None:
        ctx.telemetry.add_sink(self.sink)


async def test_host_sink_identity_unchanged_without_plugins(tmp_path: Path, skills_dir: Path) -> None:
    sink = _CollectSink()
    pool = await _pool(tmp_path, skills_dir, sink=sink)
    assert pool._hook_runner._sink is sink  # noqa: SLF001
    await pool.close()


async def test_plugin_sink_sees_events_after_joining(tmp_path: Path, skills_dir: Path) -> None:
    from taifeng.telemetry.fanout import FanoutSink

    host = _CollectSink()
    first = _SinkPlugin("first")
    second = _SinkPlugin("second", requires=("first",))
    pool = await _pool(tmp_path, skills_dir, sink=host, plugins=(second, first))
    fanout = pool._hook_runner._sink  # noqa: SLF001
    assert isinstance(fanout, FanoutSink)
    assert fanout.members[0] is host
    await pool.close()
    assert host.kinds() == [
        "plugin_loaded", "plugin_loaded", "plugin_unloaded", "plugin_unloaded",
    ]
    # first 的 sink 在自己 setup 时加入，看得到 second 的 loaded；
    # 关闭逆序：先 second（first 的 sink 仍在）再 first（自己的 sink 先被撤销）
    assert first.sink.kinds() == ["plugin_loaded", "plugin_loaded", "plugin_unloaded"]
    assert second.sink.kinds() == ["plugin_loaded"]
    assert fanout.members == (host,)


async def test_plugin_sink_works_without_host_sink(tmp_path: Path, skills_dir: Path) -> None:
    plugin = _SinkPlugin("only")
    pool = await _pool(tmp_path, skills_dir, plugins=(plugin,))
    await pool.close()
    assert plugin.sink.kinds() == ["plugin_loaded"]


async def test_failed_load_leaves_only_host_sink(tmp_path: Path, skills_dir: Path) -> None:
    host = _CollectSink()
    good = _SinkPlugin("good")
    with pytest.raises(PluginSetupError):
        await _pool(tmp_path, skills_dir, sink=host, plugins=(good, _Recording("bad", fail=True)))
    assert host.kinds() == ["plugin_loaded", "plugin_unloaded", "plugin_failed"]
    assert good.sink.kinds() == ["plugin_loaded"]
