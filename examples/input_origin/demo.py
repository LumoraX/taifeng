"""输入来源示例：单次读取标记、宿主查询与回滚后的保留；无需 API key。"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import ThreadRollback

_SKILL = """---
name: demo
description: 来源机制示例
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [fetch, inspect]
---
读取内容并检查来源。
"""


async def fetch(args: dict, ctx: taifeng.ToolContext) -> taifeng.ToolResult:
    """宿主在本次读取成功后声明来源。"""
    return taifeng.ToolResult(output="外部文档正文", origin=taifeng.InputOrigin(
        kind="tool", trust="untrusted", label="external-document"))


async def inspect(args: dict, ctx: taifeng.ToolContext) -> taifeng.ToolResult:
    """宿主读取事实，并由自己的策略决定后续操作。"""
    taint = taifeng.taint_from_extras(ctx.extras)
    assert taint.untrusted and taint.labels == ("external-document",)
    print(f"宿主看到来源：{taint.to_dict()}")
    return taifeng.ToolResult.ok("来源已交给宿主决策")


async def complete(engine: taifeng.AgentEngine, op: object) -> None:
    """等待示例提交成功收尾。"""
    submission = await engine.submit(op)  # type: ignore[arg-type]
    async for event in engine.subscribe(submission):
        if event.msg.kind == "turn_failed":
            raise RuntimeError("示例提交失败")
        if event.msg.kind == "turn_completed":
            return


async def main() -> None:
    """运行来源传播和累计保留示例。"""
    client = SimClient(turns=[
        SimTurn(tool_calls=[{"id": "f1", "name": "fetch", "arguments": "{}"}]),
        SimTurn(tool_calls=[{"id": "i1", "name": "inspect", "arguments": "{}"}]),
        SimTurn(text="已完成"),
    ])
    schema = {"type": "object", "properties": {}}
    with tempfile.TemporaryDirectory(prefix="taifeng-origin-") as directory:
        root = Path(directory)
        skills = root / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(_SKILL)
        pool = await taifeng.EnginePool.create(
            skills_dir=skills.parent, threads_dir=root / "threads", model_client=client,
            extra_tools=[taifeng.ToolSpec("fetch", "读取", schema, fetch),
                         taifeng.ToolSpec("inspect", "检查", schema, inspect)], compressors=[],
        )
        try:
            engine = await pool.get_or_create(session_id="demo", entry_skill_id="demo")
            await complete(engine, taifeng.UserMessage(text="读取文档"))
            before = engine.introspect()["input_taint"]
            submission = await engine.submit(ThreadRollback(num_turns=1))
            async for event in engine.subscribe(submission):
                if event.msg.kind == "engine_log":
                    break
            assert engine.history_snapshot() == []
            assert engine.introspect()["input_taint"] == before
            print("回滚后来源仍保留；是否确认由宿主决定。")
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
