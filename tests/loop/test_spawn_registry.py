"""K1：SpawnSlotRegistry 广度准入配额。"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.loop.spawn import SpawnLimitError, SpawnSlotRegistry


async def test_reserve_releases_concurrent_on_exit() -> None:
    reg = SpawnSlotRegistry(max_concurrent=2, max_total=100)
    async with reg.reserve():
        assert reg.snapshot()["active"] == 1
        async with reg.reserve():
            assert reg.snapshot()["active"] == 2
    # 全部退出 → active 归零；total 累计保留
    assert reg.snapshot()["active"] == 0
    assert reg.snapshot()["total"] == 2


async def test_concurrent_cap_rejects_third() -> None:
    reg = SpawnSlotRegistry(max_concurrent=2, max_total=100)
    async with reg.reserve(), reg.reserve():
        with pytest.raises(SpawnLimitError) as ei:
            async with reg.reserve():
                pass
        assert ei.value.kind == "concurrent"
    # 超限的那次未占用 slot（raise 在 yield 前）→ active 仍是 2 内退出后归零
    assert reg.snapshot()["active"] == 0


async def test_total_cap_is_monotonic_backstop() -> None:
    reg = SpawnSlotRegistry(max_concurrent=10, max_total=3)
    for _ in range(3):
        async with reg.reserve():
            pass
    # 累计已达 3，即使并发为 0 也拒绝（runaway 兜底）
    assert reg.snapshot()["active"] == 0
    with pytest.raises(SpawnLimitError) as ei:
        async with reg.reserve():
            pass
    assert ei.value.kind == "total"


async def test_concurrent_reserve_is_threadsafe_under_gather() -> None:
    """并发 fan-out（gather）下计数不超卖。"""
    reg = SpawnSlotRegistry(max_concurrent=3, max_total=100)
    peak = 0
    rejected = 0

    async def worker() -> None:
        nonlocal peak, rejected
        try:
            async with reg.reserve():
                peak = max(peak, reg.snapshot()["active"])
                await asyncio.sleep(0.02)
        except SpawnLimitError:
            rejected += 1

    await asyncio.gather(*(worker() for _ in range(10)))
    assert peak <= 3  # 并发峰值不超过 cap
    assert rejected >= 1  # 必有被拒
    assert reg.snapshot()["active"] == 0


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "sub"

    async def append(self, item: object) -> None:
        return None


async def test_run_sub_skill_rejects_when_registry_exhausted(skills_dir) -> None:
    """K1 接线：registry 耗尽 → run_sub_skill 不派发、emit 拒绝事件、返回 error。"""
    from taifeng.context.budget import ContextBudget
    from taifeng.llm.providers import MockClient
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.turn import TurnRunner
    from taifeng.skill.dispatch import CallStack, DispatchPolicy
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime
    from taifeng.tool.spec import ToolContext

    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    child = reg.get("style-checker")
    assert entry is not None and child is not None

    events: list = []

    async def _emit(ev) -> None:  # noqa: ANN001
        events.append(ev.msg)

    runner = TurnRunner(
        entry_skill=entry,
        snapshot=reg.snapshot(),
        model_client=MockClient(turns=[]),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=None,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        spawn_registry=SpawnSlotRegistry(max_total=0),  # 立刻耗尽
    )
    ctx = ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t", extras={})
    parent_stack = CallStack().push(skill_id=entry.id, call_id="e")

    result = await runner.run_sub_skill(
        target=child, arguments={}, parent_stack=parent_stack, ctx=ctx
    )
    assert result.is_error
    assert "spawn_limit_exceeded" in result.output
    rejected = [m for m in events if m.kind == "skill_spawn_rejected"]
    assert len(rejected) == 1
    assert rejected[0].data["limit_kind"] == "total"
