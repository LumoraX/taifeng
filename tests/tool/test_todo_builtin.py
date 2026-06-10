"""todo builtin 测试:TodoStore(兼 PinnedStateSource)+ todo_write 工具 + 穿越压缩 e2e。

覆盖 spec 三个 Requirement:渲染三态/空 None、整表替换幂等/非法入参显式拒绝、
双注入装配下清单穿越压缩存活(pinned:todo 注记 + 事件)。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import taifeng
from taifeng.context.pinned_state import PinnedStateSource
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers.mock import MockClient, MockTurn, RoutingMockClient
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CompactNow
from taifeng.tool.builtins.todo import TodoStore, make_todo_write_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(name="t"),
                       thread_id="t")


# ── TodoStore 渲染 ──


def test_render_three_statuses():
    """三态渲染:[ ] / [~] / [x] 前缀。"""
    store = TodoStore()
    store.replace([
        {"content": "梳理需求", "status": "completed"},
        {"content": "写实现", "status": "in_progress"},
        {"content": "补文档", "status": "pending"},
    ])
    text = store.format_for_injection()
    assert text is not None
    lines = text.splitlines()
    assert any(line.startswith("[x] 梳理需求") for line in lines[1:])
    assert any(line.startswith("[~] 写实现") for line in lines[1:])
    assert any(line.startswith("[ ] 补文档") for line in lines[1:])


def test_empty_store_returns_none():
    """空清单 → None(不注入,零噪声)。"""
    store = TodoStore()
    assert store.format_for_injection() is None


def test_store_satisfies_pinned_protocol():
    """TodoStore 满足 PinnedStateSource 运行时协议。"""
    store = TodoStore()
    assert isinstance(store, PinnedStateSource)
    assert store.name == "todo"
    assert store.max_chars > 0


# ── todo_write 工具 ──


async def test_write_replaces_and_returns_rendered():
    """整表替换 + 返回渲染清单;重复提交幂等。"""
    store = TodoStore()
    tool = make_todo_write_tool(store)
    items = [{"content": "任务A", "status": "pending"}]
    r1 = await tool.handler(  # type: ignore[misc]
        {"items": items}, _ctx())
    assert not r1.is_error
    assert "[ ] 任务A" in r1.output
    # 幂等:同 items 再提交,清单不累积
    r2 = await tool.handler({"items": items}, _ctx())  # type: ignore[misc]
    assert not r2.is_error
    assert r2.output.count("任务A") == 1
    assert len(store.items) == 1


async def test_write_invalid_status_rejected():
    """非法 status → bad_args error,清单保持原状。"""
    store = TodoStore()
    store.replace([{"content": "旧任务", "status": "pending"}])
    tool = make_todo_write_tool(store)
    r = await tool.handler(  # type: ignore[misc]
        {"items": [{"content": "x", "status": "done"}]}, _ctx())
    assert r.is_error
    assert r.data.get("reason") == "bad_args"
    assert [i["content"] for i in store.items] == ["旧任务"]


async def test_write_empty_content_rejected():
    """content 空/非字符串 → bad_args error。"""
    store = TodoStore()
    tool = make_todo_write_tool(store)
    r = await tool.handler(  # type: ignore[misc]
        {"items": [{"content": "", "status": "pending"}]}, _ctx())
    assert r.is_error
    assert r.data.get("reason") == "bad_args"
    r2 = await tool.handler({"items": "not-a-list"}, _ctx())  # type: ignore[misc]
    assert r2.is_error


def test_tool_is_not_parallel_safe():
    """写共享状态 → parallel_safe=False(串行保护)。"""
    assert make_todo_write_tool(TodoStore()).parallel_safe is False


# ── e2e:双注入装配,清单穿越压缩 ──


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
"""


async def test_todo_survives_compaction_e2e(tmp_path: Path, threads_dir: Path):
    """LLM 写清单 → CompactNow → 压缩后历史尾含 pinned:todo 注记 + 事件。"""
    skills = tmp_path / "skills"
    (skills / "planner").mkdir(parents=True)
    (skills / "planner" / "SKILL.md").write_text(_SKILL, encoding="utf-8")

    store = TodoStore()
    # handoff 摘要采样的 prompt 不含技能标记 → 摘要走独立 MockClient
    summary_client = MockClient(turns=[MockTurn(text="## 摘要")])
    client = RoutingMockClient(routes={
        "PLANNER_MARK": [
            MockTurn(text="记录清单", tool_calls=[
                {"id": "t1", "name": "todo_write", "arguments":
                 '{"items":[{"content":"训练玄武岩模型","status":"pending"}]}'}]),
            MockTurn(text="已记录"),
            MockTurn(text="继续聊别的 a"),
            MockTurn(text="继续聊别的 b"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[HandoffCompactionStrategy(model_client=summary_client)],
        extra_tools=[make_todo_write_tool(store)],   # ← 双注入:同一 store
        pinned_state_sources=[store],
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="planner")
    events: list = []

    async def watch():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    for q in ("帮我规划任务", "再聊点别的", "再聊一轮"):
        sub_id = await engine.submit(taifeng.UserMessage(text=q))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

    sub_id = await engine.submit(CompactNow(force=True))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("compaction_completed", "turn_failed"):
            break

    pinned = [it for it in engine.history_snapshot()
              if it.kind == "system_injection"
              and it.payload.get("source") == "pinned:todo"]
    assert pinned, "清单未穿越压缩"
    assert "玄武岩" in pinned[-1].payload["text"]
    ev2 = next(m for m in events if m.kind == "pinned_state_reinjected")
    assert any(s["name"] == "todo" for s in ev2.data["sources"])
    task.cancel()
    await pool.close()
