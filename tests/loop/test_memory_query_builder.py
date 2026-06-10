"""memory_query_builder 注入点测试:自定义语境 / 未注入零变化 / 崩溃回退。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import ResponseItem, user_message
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _RecordingMemory:
    """只记录 prefetch 收到的 query。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        self.queries.append(query)
        return ""

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        return None

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        return ""

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        return None


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


async def _run(skills_dir: Path, *, builder=None) -> _RecordingMemory:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    mem = _RecordingMemory()

    async def _emit(ev: object) -> None:
        return None

    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=SimClient(turns=[SimTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()), store=_FakeStore(),
        compressors=None, dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t", submission_id="s", emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=[
            user_message("第一轮:背景说明", thread_id="t"),
            user_message("第二轮:具体提问", thread_id="t"),
        ],
        memory_store=mem,
        memory_query_builder=builder,
    )
    await runner.run()
    return mem


async def test_custom_builder_receives_history_copy(skills_dir: Path) -> None:
    """builder 拿到 history 拷贝并自由构造语境;旁改不影响内核 buffer。"""
    captured: list[list[ResponseItem]] = []

    def builder(history: list[ResponseItem]) -> str:
        captured.append(list(history))
        query = " | ".join(
            str(it.payload.get("text", "")) for it in history[-3:])
        history.clear()  # 旁改拷贝 —— 不得影响内核后续流程
        return query

    mem = await _run(skills_dir, builder=builder)
    assert mem.queries == ["第一轮:背景说明 | 第二轮:具体提问"]
    assert len(captured[0]) == 2  # builder 收到完整两条历史


async def test_no_builder_keeps_legacy_query(skills_dir: Path) -> None:
    """未注入 → 既有行为逐字不变(最后一条 user_message 文本)。"""
    mem = await _run(skills_dir)
    assert mem.queries == ["第二轮:具体提问"]


async def test_builder_crash_falls_back_with_log(skills_dir: Path) -> None:
    """builder 抛异常 → 记日志 + 回退默认构造,turn 正常。"""

    def builder(history: list[ResponseItem]) -> str:
        raise RuntimeError("builder exploded")

    mem = await _run(skills_dir, builder=builder)
    assert mem.queries == ["第二轮:具体提问"]  # 回退默认
