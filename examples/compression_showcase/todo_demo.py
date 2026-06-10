"""todo builtin demo —— LLM 自管任务清单穿越压缩(mock,无需 key)。

pinned_demo.py 演示的是业务自实现 ``PinnedStateSource``;本 demo 演示**内置
范例** ``TodoStore`` + ``todo_write``:LLM 经工具整表维护清单,双注入装配后
清单自动穿越压缩(hermes todo_tool / Claude Code TodoWrite 范式)。

装配只有两行(同一 store 双注入):

    store = TodoStore()
    pool = await taifeng.EnginePool.create(
        ...,
        extra_tools=[make_todo_write_tool(store)],
        pinned_state_sources=[store],
    )

运行:
    PYTHONPATH=src uv run python examples/compression_showcase/todo_demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers.mock import MockClient, MockTurn, RoutingMockClient
from taifeng.loop.submission import CompactNow
from taifeng.tool.builtins.todo import TodoStore, make_todo_write_tool

_SKILL = """---
name: planner
description: 规划助手
version: 1.0.0
type: composite
entry: true
tool_names: [todo_write]
max_call_depth: 2
---
# PLANNER_MARK 规划助手
用 todo_write 维护任务清单。
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "skills" / "planner").mkdir(parents=True)
        (root / "skills" / "planner" / "SKILL.md").write_text(
            _SKILL, encoding="utf-8")

        store = TodoStore()
        client = RoutingMockClient(routes={
            "PLANNER_MARK": [
                # turn1:建立清单
                MockTurn(text="建立清单", tool_calls=[
                    {"id": "t1", "name": "todo_write", "arguments":
                     '{"items":['
                     '{"content":"梳理需求边界","status":"in_progress"},'
                     '{"content":"实现核心模块","status":"pending"},'
                     '{"content":"补齐测试","status":"pending"}]}'}]),
                MockTurn(text="清单已建立"),
                # turn2:推进进度(整表替换)
                MockTurn(text="更新进度", tool_calls=[
                    {"id": "t2", "name": "todo_write", "arguments":
                     '{"items":['
                     '{"content":"梳理需求边界","status":"completed"},'
                     '{"content":"实现核心模块","status":"in_progress"},'
                     '{"content":"补齐测试","status":"pending"}]}'}]),
                MockTurn(text="进度已更新"),
                MockTurn(text="好的,继续。"),
            ],
        })
        pool = await taifeng.EnginePool.create(
            skills_dir=root / "skills", threads_dir=root / "threads",
            model_client=client,
            compressors=[HandoffCompactionStrategy(
                model_client=MockClient(turns=[MockTurn(text="## 会话摘要")]))],
            extra_tools=[make_todo_write_tool(store)],  # ← 双注入:同一 store
            pinned_state_sources=[store],
        )
        engine = await pool.get_or_create(
            session_id="demo", entry_skill_id="planner")

        for q in ("规划这个项目", "推进一步", "随便聊聊"):
            sub_id = await engine.submit(taifeng.UserMessage(text=q))
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break
        print(f"[1] LLM 两次 todo_write 后的清单:\n{store.format_for_injection()}")

        # 强制压缩 → 清单自动钉回
        sub_id = await engine.submit(CompactNow(force=True))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind == "pinned_state_reinjected":
                print(f"[2] pinned_state_reinjected: sources={ev.msg.data['sources']}")
            if ev.msg.kind in ("compaction_completed", "turn_failed"):
                break

        pinned = [it for it in engine.history_snapshot()
                  if it.kind == "system_injection"
                  and it.payload.get("source") == "pinned:todo"]
        assert pinned, "清单未穿越压缩!"
        print("[3] 压缩后历史尾部的清单注记:")
        for line in pinned[-1].payload["text"].splitlines():
            print(f"      {line}")
        await pool.close()
        print("\n✅ demo 完成:LLM 自管清单经 todo_write 维护并自动穿越压缩")


if __name__ == "__main__":
    asyncio.run(main())
