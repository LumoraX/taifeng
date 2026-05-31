"""K7：子 skill 派发把谱系（父 thread / spawn 深度 / 栈路径）持久化进 ThreadMetadata.extra。

使 resume 可从持久谱系重导子 agent 深度/特权（不改"独立 seed"隔离模型——
子 turn 仍拿干净 seed，这是 taifeng 的刻意隔离设计，非缺口）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import CallStack, DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolContext


class _RecStore:
    """记录 create_thread(extra=...) 调用的假 store。"""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []

    async def create_thread(
        self, *, cwd=None, entry_skill_id=None, source=None, extra=None,  # noqa: ANN001
    ) -> str:
        self.create_calls.append(
            {"entry_skill_id": entry_skill_id, "source": source, "extra": extra}
        )
        return "sub-thread"

    async def append(self, item: object) -> None:
        return None


@pytest.mark.asyncio
async def test_sub_skill_persists_parent_lineage_into_extra(skills_dir: Path) -> None:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    child = reg.get("style-checker")
    assert entry is not None and child is not None

    rec = _RecStore()

    async def _emit(_ev: object) -> None:
        return None

    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=MockClient(turns=[MockTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()), store=rec,
        compressors=None, dispatch_policy=DispatchPolicy(), budget=ContextBudget(),
        thread_id="parent-thread", submission_id="s", emit=_emit,
        cancel=CancellationToken(name="t"),
    )
    parent_stack = CallStack().push(skill_id=entry.id, call_id="e")
    ctx = ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="parent-thread")

    await runner.run_sub_skill(
        target=child, arguments={"x": 1}, parent_stack=parent_stack, ctx=ctx
    )

    assert len(rec.create_calls) == 1
    extra = rec.create_calls[0]["extra"]
    assert extra["parent_thread_id"] == "parent-thread"
    assert extra["spawn_depth"] == parent_stack.depth  # 持久化深度，resume 可重导
    assert extra["stack_path"] == parent_stack.path()
    # source 旧约定保持不变（非破坏）
    assert rec.create_calls[0]["source"] == "subskill:code-reviewer"
