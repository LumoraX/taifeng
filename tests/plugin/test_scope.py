"""pool 之间无泄漏：两个 pool 各加载同 id 插件，四类注册互不可见（plugin-isolation-and-release）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.hooks.types import HookDecision
from taifeng.llm.providers.sim import SimClient
from taifeng.loop.pool import EnginePool
from taifeng.plugin import PluginContext, PluginSpec
from taifeng.tool.spec import ToolResult, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.loop.event import EventMsg


class _Sink:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def handle(self, ev: EventMsg) -> None:
        self.kinds.append(ev.msg.kind)


class _Everything:
    """同 id 插件：注册工具、钩子、skill 目录与 sink，工具名带上 tag 以区分来源。"""

    spec = PluginSpec(id="same-id", version="1")

    def __init__(self, tag: str, skills_dir: Path) -> None:
        self.tag = tag
        self.skills_dir = skills_dir
        self.sink = _Sink()

    async def setup(self, ctx: PluginContext) -> None:
        async def handler(args: dict[str, Any], tool_ctx: Any) -> ToolResult:
            return ToolResult.ok(self.tag)

        async def hook(payload: Any, hook_ctx: Any) -> HookDecision:
            return HookDecision.ok()

        ctx.tools.register(ToolSpec(
            name=f"tool_{self.tag}", description="d", input_schema={"type": "object"}, handler=handler,
        ))
        ctx.hooks.register("pre_turn", hook)
        await ctx.skills.add_dir(self.skills_dir, source="user")
        ctx.telemetry.add_sink(self.sink)


def _skill_dir(root: Path, tag: str) -> Path:
    directory = root / f"plugin-{tag}"
    (directory / f"skill-{tag}").mkdir(parents=True)
    (directory / f"skill-{tag}" / "SKILL.md").write_text(
        f"---\nname: skill-{tag}\ndescription: d\ntype: atomic\n---\n# {tag}\n", encoding="utf-8",
    )
    return directory


async def _pool(root: Path, skills_dir: Path, plugin: _Everything) -> EnginePool:
    return await EnginePool.create(
        skills_dir=skills_dir, threads_dir=root / f"threads-{plugin.tag}",
        model_client=SimClient(turns=[]), compressors=[], plugins=(plugin,),
    )


def _visible(pool: EnginePool, tag: str) -> dict[str, bool]:
    return {
        "tool": pool.tool_registry.get(f"tool_{tag}") is not None,
        "skill": pool.skill_registry.get(f"skill-{tag}") is not None,
    }


async def test_two_pools_do_not_share_registrations(tmp_path: Path, skills_dir: Path) -> None:
    a = _Everything("a", _skill_dir(tmp_path, "a"))
    b = _Everything("b", _skill_dir(tmp_path, "b"))
    pool_a = await _pool(tmp_path, skills_dir, a)
    pool_b = await _pool(tmp_path, skills_dir, b)

    assert _visible(pool_a, "a") == {"tool": True, "skill": True}
    assert _visible(pool_a, "b") == {"tool": False, "skill": False}
    assert _visible(pool_b, "b") == {"tool": True, "skill": True}
    assert _visible(pool_b, "a") == {"tool": False, "skill": False}
    hooks_a = pool_a._hooks.registry.handlers("pre_turn")  # noqa: SLF001
    hooks_b = pool_b._hooks.registry.handlers("pre_turn")  # noqa: SLF001
    assert len(hooks_a) == len(hooks_b) == 1
    assert hooks_a[0] is not hooks_b[0]

    await pool_a.close()
    # 关闭 A 不影响 B：B 的四类注册全部仍在
    assert _visible(pool_b, "b") == {"tool": True, "skill": True}
    assert len(pool_b._hooks.registry.handlers("pre_turn")) == 1  # noqa: SLF001
    sink_b_before = list(b.sink.kinds)
    await pool_b.close()
    # A 的 sink 只见过 A 的事件，B 的 sink 只见过 B 的事件（两边都只有自己一个插件）
    assert a.sink.kinds == ["plugin_loaded"]
    assert sink_b_before == ["plugin_loaded"]
    assert _visible(pool_b, "b") == {"tool": False, "skill": False}
