"""插件系统示例 —— 三个通用插件经 ``EnginePool.create(plugins=...)`` 装配（🧪 ADR 0144 / 0145）。

- ``ExtraSkillsPlugin``：挂上一个 skill 目录（入口 skill ``plugin-assistant``），``source="marketplace"``；
- ``ReadOnlyToolPlugin``：注册无副作用工具 ``word_count``，依赖 ExtraSkillsPlugin（演示 ``requires``）；
- ``JsonlAuditSinkPlugin``：订阅 pool 级事件（插件生命周期等），写入构造时注入的文件。

模型用 SimClient 脚本：先调 ``word_count``，再给出答案。pool 关闭后三个插件的注册全部撤销。

运行：
    PYTHONPATH=src python examples/plugin_system/demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import taifeng
from taifeng.experimental import PluginContext, PluginSpec
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.tool.spec import ToolResult, ToolSpec

HOST_SKILL = """---
name: host-entry
description: 宿主自己的入口 skill
type: composite
entry: true
tool_names: [shell_exec]
---
# 宿主入口
"""

PLUGIN_ENTRY = """---
name: plugin-assistant
description: 由插件挂上的入口 skill，会数词
type: composite
entry: true
tool_names: [word_count]
child_skills: [plugin-notes]
---
# 插件助手
需要统计词数时调用 `word_count(text)`。
"""

PLUGIN_NOTES = """---
name: plugin-notes
description: 插件附带的说明
type: atomic
---
# 说明
词数按空白切分。
"""


class ExtraSkillsPlugin:
    """挂上一个 skill 目录。"""

    spec = PluginSpec(id="extra-skills", version="1.0")

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def setup(self, ctx: PluginContext) -> None:
        await ctx.skills.add_dir(self._directory, source="marketplace")


class ReadOnlyToolPlugin:
    """注册一个无副作用的工具。"""

    spec = PluginSpec(id="word-tools", version="1.0", requires=("extra-skills",))

    async def setup(self, ctx: PluginContext) -> None:
        async def word_count(args: dict[str, Any], tool_ctx: Any) -> ToolResult:
            return ToolResult.ok(str(len(str(args.get("text", "")).split())))

        ctx.tools.register(ToolSpec(
            name="word_count",
            description="统计文本里的词数",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=word_count,
            parallel_safe=True,
        ))


class JsonlAuditSinkPlugin:
    """把 pool 级事件写成 JSONL（路径由宿主注入）。"""

    spec = PluginSpec(id="jsonl-audit", version="1.0")

    def __init__(self, path: Path) -> None:
        self._path = path
        self.kinds: list[str] = []

    async def setup(self, ctx: PluginContext) -> None:
        ctx.telemetry.add_sink(self)

    async def handle(self, ev: taifeng.EventMsg) -> None:
        self.kinds.append(ev.msg.kind)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": ev.msg.kind, "data": ev.msg.data}, ensure_ascii=False) + "\n")


def _write_skills(root: Path) -> tuple[Path, Path]:
    host = root / "host-skills"
    (host / "host-entry").mkdir(parents=True)
    (host / "host-entry" / "SKILL.md").write_text(HOST_SKILL, encoding="utf-8")
    extra = root / "plugin-skills"
    for name, body in (("plugin-assistant", PLUGIN_ENTRY), ("plugin-notes", PLUGIN_NOTES)):
        (extra / name).mkdir(parents=True)
        (extra / name / "SKILL.md").write_text(body, encoding="utf-8")
    return host, extra


async def run(root: Path) -> dict[str, Any]:
    """跑一次完整流程，返回可核验的结果（供 tests/examples 复用）。"""
    host, extra = _write_skills(root)
    audit = JsonlAuditSinkPlugin(root / "pool-events.jsonl")
    client = SimClient(turns=[
        SimTurn(text="", tool_calls=[{
            "id": "c1", "name": "word_count", "arguments": json.dumps({"text": "plugins keep the kernel small"}),
        }]),
        SimTurn(text="这句话一共 5 个词。"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=host, threads_dir=root / "threads", model_client=client, compressors=[],
        plugins=(audit, ReadOnlyToolPlugin(), ExtraSkillsPlugin(extra)),
    )
    entry = pool.skill_registry.get("plugin-assistant")
    engine = await pool.get_or_create(session_id="demo", entry_skill_id="plugin-assistant")
    sub_id = await engine.submit(taifeng.UserMessage(text="数一下词"))
    tool_output = ""
    final = ""
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind == "tool_call_completed":
            tool_output = str(ev.msg.data.get("output", ""))
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            final = ev.msg.kind
            break
    await pool.close()
    return {
        "entry_source": entry.source if entry is not None else None,
        "tool_output": tool_output,
        "final": final,
        "pool_events": audit.kinds,
        "tool_after_close": pool.tool_registry.get("word_count") is not None,
        "skill_after_close": pool.skill_registry.get("plugin-assistant") is not None,
    }


def report(result: dict[str, Any]) -> bool:
    """打印结果；返回是否全部符合预期。"""
    checks = {
        "插件 skill 来源为 marketplace": result["entry_source"] == "marketplace",
        "插件工具被调用并返回 5": result["tool_output"].strip().startswith("5"),
        "turn 正常结束": result["final"] == "turn_completed",
        "审计 sink 收到插件生命周期事件": result["pool_events"].count("plugin_loaded") == 3,
        "关闭后工具已撤销": not result["tool_after_close"],
        "关闭后 skill 目录已摘除": not result["skill_after_close"],
    }
    for name, ok in checks.items():
        print(f"  {'✅' if ok else '✗ 未通过'} {name}")
    return all(checks.values())


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        result = asyncio.run(run(Path(td)))
    print(f"pool 级事件：{result['pool_events']}")
    ok = report(result)
    print("✅ plugin_system demo 通过" if ok else "✗ plugin_system demo 未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
