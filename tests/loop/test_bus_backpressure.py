"""K4：总线流控 —— 入站 submission 背压 + 出站事件丢弃计数（非静默）。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import SimClient
from taifeng.loop.event import EngineLog, EventMsg
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path


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
        model_client=SimClient(turns=[]),
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
    from taifeng.loop.engine import _Subscriber

    engine = await _make_engine(skills_dir, event_queue_size=1)
    # 手动注册一个已满的 firehose 订阅者（_Subscriber 内队列容量 1，预先占满）
    sub = _Subscriber(maxsize=1, high_ratio=0.75, low_ratio=0.5)
    sub.queue.put_nowait("occupied")  # type: ignore[arg-type]
    engine._all_subs.append(sub)  # noqa: SLF001

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
    # 逃生口的**结构性**证据：maxsize==0 即 asyncio.Queue 的「不限」语义。
    # 原来只靠 `wait_for(..., timeout=0.5)` 侧面证明「没阻塞」——引擎没 run、队列
    # 不被 drain，一旦退化成 bounded 就是永久阻塞，所以守卫期限放宽不影响判定力；
    # 但直接断言 maxsize 更快失败、也更说明意图。
    assert engine._submissions.maxsize == 0, (  # noqa: SLF001
        f"size<=0 应映射为不限队列，实得 maxsize={engine._submissions.maxsize}"  # noqa: SLF001
    )
    for i in range(50):
        await asyncio.wait_for(
            engine.submit(taifeng.UserMessage(text=str(i))),
            timeout=GUARD_TIMEOUT_SECONDS,
        )  # 不阻塞
