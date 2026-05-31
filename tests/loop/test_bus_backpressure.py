"""K4：总线流控 —— 入站 submission 背压 + 出站事件丢弃计数（非静默）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import MockClient
from taifeng.loop.event import EngineLog, EventMsg
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


async def _make_engine(skills_dir: Path, **kw: object) -> taifeng.AgentEngine:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    return taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=reg.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=MockClient(turns=[]),
        store=_FakeStore(),
        thread_id="t",
        **kw,
    )


@pytest.mark.asyncio
async def test_submission_queue_backpressure(skills_dir: Path) -> None:
    """bounded submission 队列 + 未运行 → 满后 submit 阻塞（入站背压）。"""
    engine = await _make_engine(skills_dir, submission_queue_size=1)
    # 引擎未 run，队列不被 drain。第 1 条占满（size=1）
    await engine.submit(taifeng.UserMessage(text="1"))
    # 第 2 条应阻塞（背压）→ wait_for 超时
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            engine.submit(taifeng.UserMessage(text="2")), timeout=0.15
        )


@pytest.mark.asyncio
async def test_event_drop_is_counted_not_silent(skills_dir: Path) -> None:
    """订阅队列满 → 丢弃被计数（events_dropped），非静默。"""
    engine = await _make_engine(skills_dir, event_queue_size=1)
    # 手动注册一个已满的 all-sub 队列
    full_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    full_q.put_nowait("occupied")
    engine._all_subs.append(full_q)  # noqa: SLF001

    assert engine.events_dropped == 0
    await engine._emit(  # noqa: SLF001
        EventMsg(submission_id="s", msg=EngineLog(data={"level": "info"}))
    )
    assert engine.events_dropped == 1
    # 再丢一条 → 计数累加
    await engine._emit(  # noqa: SLF001
        EventMsg(submission_id="s", msg=EngineLog(data={"level": "info"}))
    )
    assert engine.events_dropped == 2


@pytest.mark.asyncio
async def test_submission_unbounded_when_size_zero(skills_dir: Path) -> None:
    """submission_queue_size<=0 → 不限（逃生口），submit 不阻塞。"""
    engine = await _make_engine(skills_dir, submission_queue_size=0)
    for i in range(50):
        await asyncio.wait_for(
            engine.submit(taifeng.UserMessage(text=str(i))), timeout=0.5
        )  # 不阻塞
