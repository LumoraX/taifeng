"""pinned-periodic —— pinned 状态（如 todo 清单）按用户轮数周期重注。

回归点：清单只在压缩后钉回，长会话里早已淹没在历史中段，模型工作记忆失焦。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.context.pinned_state import (
    PeriodicPinnedStateSource,
    PinnedStateRegistry,
    pinned_injection_source,
    turns_since_injection,
)
from taifeng.conversation.models import assistant_message, system_injection, user_message
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.tool.builtins.todo import TodoStore

if TYPE_CHECKING:
    from pathlib import Path


def test_turns_since_injection_counts_user_messages_after_marker() -> None:
    history = [
        user_message("1", thread_id="t"),
        system_injection("清单", thread_id="t", source=pinned_injection_source("todo")),
        user_message("2", thread_id="t"),
        assistant_message("a", thread_id="t", model="m"),
        user_message("3", thread_id="t"),
    ]
    assert turns_since_injection(history, "todo") == 2
    assert turns_since_injection(history, "other") == 3  # 从未注入 → 全部用户消息数


def _store(every: int | None) -> TodoStore:
    store = TodoStore(reinject_every_turns=every)
    store.replace([{"content": "写报告", "status": "in_progress"}])
    return store


def test_todo_store_is_periodic_source() -> None:
    assert isinstance(_store(3), PeriodicPinnedStateSource)


def test_render_due_only_renders_sources_past_cadence() -> None:
    registry = PinnedStateRegistry()
    registry.register(_store(3))
    assert registry.render_due(lambda name: 2).entries == []
    (entry,) = registry.render_due(lambda name: 3).entries
    assert entry.name == "todo" and "[~] 写报告" in entry.text


def test_render_due_skips_sources_without_cadence() -> None:
    registry = PinnedStateRegistry()
    registry.register(_store(None))
    assert registry.render_due(lambda name: 100).entries == []


async def _run_turns(tmp_path: Path, store: TodoStore, n: int) -> tuple[list[Any], list[Any]]:
    """跑 n 轮纯文本 turn，返回 (pinned 事件, 最终 history)。"""
    skills = tmp_path / "skills" / "agent"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: agent\ndescription: d\ntype: composite\nentry: true\n"
        "tool_names: [todo_write]\n---\n# agent\n", encoding="utf-8")
    pool = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", threads_dir=tmp_path / "threads",
        model_client=SimClient(turns=[SimTurn(text=f"答{i}") for i in range(n)]),
        compressors=[], pinned_state_sources=[store])
    engine = await pool.get_or_create(session_id="s", entry_skill_id="agent")
    events: list[Any] = []
    for i in range(n):
        sub_id = await engine.submit(taifeng.UserMessage(text=f"第{i}轮"))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind == "pinned_state_reinjected":
                events.append(ev.msg)
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed", ev.msg.data
                break
    history = engine.history_snapshot()
    await pool.close()
    return events, history


async def test_periodic_reinjection_every_two_turns(tmp_path: Path) -> None:
    """节奏 2：第 2、4 轮开头各注入一次清单，事件 phase=periodic。"""
    events, history = await _run_turns(tmp_path, _store(2), 4)
    assert [e.data["phase"] for e in events] == ["periodic", "periodic"]
    marker = pinned_injection_source("todo")
    kinds = [(it.kind, it.payload.get("source")) for it in history
             if it.kind in ("user_message", "system_injection")]
    # 注入紧随第 2、4 条用户消息
    assert kinds == [
        ("user_message", None), ("user_message", None), ("system_injection", marker),
        ("user_message", None), ("user_message", None), ("system_injection", marker),
    ]


async def test_no_cadence_means_no_periodic_injection(tmp_path: Path) -> None:
    events, _ = await _run_turns(tmp_path, _store(None), 3)
    assert events == []
