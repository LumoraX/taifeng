"""K2：会话级 token 上限强制（OOM-killer）—— 转内中止 + 跨 turn 拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


def test_session_limit_exceeded_helper(skills_dir: Path) -> None:
    import anyio

    reg = anyio.run(lambda: FilesystemSkillRegistry.load(skills_dir))
    entry = reg.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: object) -> None:
        return None

    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=MockClient(turns=[]), tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(), compressors=None, dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(), thread_id="t", submission_id="s",
        emit=_emit, cancel=CancellationToken(name="t"),
        session_tokens_used=80, max_session_tokens=100,
    )
    assert runner._session_limit_exceeded() is False  # 80 < 100  # noqa: SLF001
    runner.total_usage = TokenUsage(input_tokens=30, total_tokens=30)
    assert runner._session_limit_exceeded() is True  # 80+30 >= 100  # noqa: SLF001
    runner.max_session_tokens = None
    assert runner._session_limit_exceeded() is False  # 未配置→不强制  # noqa: SLF001


@pytest.mark.asyncio
async def test_turn_aborts_when_token_ceiling_hit_with_pending_work(
    skills_dir: Path,
) -> None:
    """超限且仍有 tool call → 中止本 turn（不再采样下一轮）。"""
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    # 一个带 tool_call 的 turn，usage 直接超 100；tool 不存在→unknown_tool（不影响）
    client = MockClient(turns=[
        MockTurn(
            text="", tool_calls=[{"id": "c1", "name": "nope", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=200, total_tokens=200),
        ),
        MockTurn(text="should-not-reach"),
    ])
    events: list = []

    async def _emit(ev) -> None:  # noqa: ANN001
        events.append(ev.msg)

    runner = TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=client, tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(), compressors=None, dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(), thread_id="t", submission_id="s",
        emit=_emit, cancel=CancellationToken(name="t"),
        session_tokens_used=0, max_session_tokens=100,
    )
    outcome = await runner.run()
    assert outcome.end_reason == "resource_limit_exceeded"
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert len(rl) == 1
    assert rl[0].data["scope"] == "turn_aborted"
    assert client._idx == 1  # 第二个 turn 未被采样  # noqa: SLF001


@pytest.mark.asyncio
async def test_engine_refuses_new_turn_after_session_limit(
    skills_dir: Path, threads_dir: Path
) -> None:
    """跨 turn：turn1 耗尽预算 → turn2 在 pre-turn 守卫被拒。"""
    client = MockClient(turns=[
        MockTurn(text="一", usage=TokenUsage(input_tokens=200, total_tokens=200)),
        MockTurn(text="二", usage=TokenUsage(input_tokens=10, total_tokens=10)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], max_session_tokens=100,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    sub1 = await engine.submit(taifeng.UserMessage(text="hi"))
    async for ev in engine.subscribe(sub1):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    events: list = []
    sub2 = await engine.submit(taifeng.UserMessage(text="hi2"))
    async for ev in engine.subscribe(sub2):
        events.append(ev.msg)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    failed = next(m for m in events if m.kind == "turn_failed")
    assert failed.data["kind"] == "resource_limit_exceeded"
    rl = [m for m in events if m.kind == "resource_limit_exceeded"]
    assert rl and rl[0].data["scope"] == "turn_refused"

    await pool.close()
