"""可配置运行时参数测试：max_iterations / UpdateBudget / ThreadRollback / CompactNow params。"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import (
    CompactNow,
    RefreshSnapshot,
    ThreadRollback,
    UpdateBudget,
)


@pytest.mark.asyncio
async def test_engine_exposes_budget_and_history(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[MockTurn(text="hi", usage=TokenUsage(input_tokens=10, output_tokens=5))])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    assert engine.budget.context_window == 200_000
    assert engine.history_snapshot() == []
    assert engine.estimate_tokens() == 0
    assert engine.usage_ratio() == 0.0
    await pool.close()


@pytest.mark.asyncio
async def test_max_iterations_configurable(skills_dir: Path, threads_dir: Path) -> None:
    # 模拟一个永远调 tool 的 LLM
    turns = [
        MockTurn(
            text="t",
            tool_calls=[{"id": f"c{i}", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        for i in range(20)
    ]
    client = MockClient(turns=turns)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], max_iterations=3,
    )
    engine = await pool.get_or_create(session_id="s_max", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="loop"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.data.get("end_reason") == "max_iterations"
            assert ev.msg.data.get("iterations") == 3
            break
    await pool.close()


@pytest.mark.asyncio
async def test_update_budget_at_runtime(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[MockTurn(text="hi")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_b", entry_skill_id="code-reviewer")

    assert engine.budget.context_window == 200_000
    assert engine.budget.soft_limit_ratio == pytest.approx(0.85)

    await engine.submit(UpdateBudget(context_window=50_000, soft_limit_ratio=0.7))
    # actor loop 处理 op 需要时间 —— 拉一条心跳让它走过
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.01)
        if engine.budget.context_window == 50_000:
            break
    assert engine.budget.context_window == 50_000
    assert engine.budget.soft_limit_ratio == pytest.approx(0.7)
    # 未传字段保持原值
    assert engine.budget.preserve_tail_messages == 4
    await pool.close()


@pytest.mark.asyncio
async def test_thread_rollback_drops_recent_turns(
    skills_dir: Path, threads_dir: Path
) -> None:
    client = MockClient(
        turns=[
            MockTurn(text="t1"),
            MockTurn(text="t2"),
            MockTurn(text="t3"),
        ]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_r", entry_skill_id="code-reviewer")

    # 跑 3 轮
    for q in ("Q1", "Q2", "Q3"):
        sub_id = await engine.submit(taifeng.UserMessage(text=q))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

    initial_count = len(engine.history_snapshot())
    initial_users = sum(1 for it in engine.history_snapshot() if it.kind == "user_message")
    assert initial_users == 3

    # 回滚 1 轮
    await engine.submit(ThreadRollback(num_turns=1))
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.01)
        if (
            sum(1 for it in engine.history_snapshot() if it.kind == "user_message")
            < initial_users
        ):
            break

    snap = engine.history_snapshot()
    users_after = sum(1 for it in snap if it.kind == "user_message")
    assert users_after == 2  # 回滚掉 Q3
    assert len(snap) < initial_count
    await pool.close()


@pytest.mark.asyncio
async def test_compact_now_with_params(skills_dir: Path, threads_dir: Path) -> None:
    """CompactNow(force=True, preserve_tail=2) 即使未达阈值也强制压缩。"""
    from taifeng.context.strategies import HandoffCompactionStrategy

    summary_client = MockClient(
        turns=[MockTurn(text="## 摘要", usage=TokenUsage(input_tokens=100, output_tokens=10))]
    )
    # 主 chat 用 mock 也行
    main_client = MockClient(
        turns=[MockTurn(text=f"reply {i}") for i in range(10)]
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=main_client,
        compressors=[HandoffCompactionStrategy(model_client=summary_client)],
    )
    engine = await pool.get_or_create(session_id="s_c", entry_skill_id="code-reviewer")
    # 跑几轮制造历史
    for q in ("a", "b", "c", "d", "e"):
        sub_id = await engine.submit(taifeng.UserMessage(text=q))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

    before_count = len(engine.history_snapshot())
    assert before_count >= 5

    # 强制压缩
    sub_id = await engine.submit(CompactNow(force=True, preserve_tail=2))

    # 等 actor 处理 + compaction 完成
    import asyncio
    for _ in range(200):
        await asyncio.sleep(0.01)
        snap = engine.history_snapshot()
        if any(it.kind == "compacted" for it in snap):
            break

    snap = engine.history_snapshot()
    compacted_count = sum(1 for it in snap if it.kind == "compacted")
    assert compacted_count >= 1, f"expected compacted item, got history: {[it.kind for it in snap]}"
    await pool.close()


# ====================================================================
# config-consistency-fixes C2 — event_queue_size kwarg 真正生效
# 之前 AgentEngine.__init__ 收下 event_queue_size 但未存 self，
# subscribe / subscribe_all 仍硬编码 maxsize=1024，用户传值无效。
# ====================================================================


@pytest.mark.asyncio
async def test_event_queue_size_kwarg_takes_effect(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """通过 EnginePool.create 传 event_queue_size=42 → engine 内部新建的订阅
    queue maxsize 必须等于 42，subscribe 与 subscribe_all 都生效。

    实现注：subscribe() 的 finally 在协程被取消时会 pop _event_subs，所以
    必须在 cancel 之前抓到 queue 引用并断言。这里 spawn task + sleep 让
    generator 推进到 q.get() 阻塞点，期间 queue 已注册。
    """
    import asyncio

    client = MockClient(turns=[MockTurn(text="hi")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
        event_queue_size=42,
    )
    engine = await pool.get_or_create(
        session_id="s_eqs", entry_skill_id="code-reviewer",
    )
    # engine 实例真正存了配置（之前 dead config 时 self 上没有该属性）
    assert engine._event_queue_size == 42  # noqa: SLF001

    # 1) subscribe(sub_id) 新建的 per-submission queue 用配置值
    async def _sub_runner() -> None:
        async for _ in engine.subscribe("queue-size-probe"):
            pass

    sub_task = asyncio.create_task(_sub_runner())
    # 让 subscribe 协程跑到 q.get() 阻塞点（已注册到 _event_subs）
    for _ in range(20):
        await asyncio.sleep(0.005)
        if "queue-size-probe" in engine._event_subs:  # noqa: SLF001
            break

    assert "queue-size-probe" in engine._event_subs, (  # noqa: SLF001
        "subscribe 应注册到 _event_subs"
    )
    per_sub_q = engine._event_subs["queue-size-probe"]  # noqa: SLF001
    assert per_sub_q.maxsize == 42, (
        f"per-submission queue maxsize 应为 42（配置值），"
        f"实际 {per_sub_q.maxsize} —— 若是 1024 说明回退到硬编码"
    )
    sub_task.cancel()
    try:
        await sub_task
    except (asyncio.CancelledError, Exception):
        pass

    # 2) subscribe_all() 新建的 broadcast queue 用配置值
    async def _all_runner() -> None:
        async for _ in engine.subscribe_all():
            pass

    all_task = asyncio.create_task(_all_runner())
    for _ in range(20):
        await asyncio.sleep(0.005)
        if engine._all_subs:  # noqa: SLF001
            break

    assert engine._all_subs, (  # noqa: SLF001
        "subscribe_all 应注册一个 broadcast queue"
    )
    all_q = engine._all_subs[-1]  # noqa: SLF001
    assert all_q.maxsize == 42, (
        f"broadcast queue maxsize 应为 42，实际 {all_q.maxsize}"
    )
    all_task.cancel()
    try:
        await all_task
    except (asyncio.CancelledError, Exception):
        pass

    await pool.close()


@pytest.mark.asyncio
async def test_event_queue_size_default_is_1024(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """不传 event_queue_size → 默认 1024（向后兼容）。"""
    client = MockClient(turns=[MockTurn(text="hi")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s_def", entry_skill_id="code-reviewer",
    )
    assert engine._event_queue_size == 1024  # noqa: SLF001
    await pool.close()


@pytest.mark.asyncio
async def test_refresh_snapshot_op(skills_dir: Path, threads_dir: Path) -> None:
    client = MockClient(turns=[MockTurn(text="hi")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_rs", entry_skill_id="code-reviewer")
    v0 = engine.snapshot.version

    # 模拟外部加新 skill
    (skills_dir / "extra-skill").mkdir()
    (skills_dir / "extra-skill" / "SKILL.md").write_text(
        "---\nname: extra-skill\ndescription: x\ntype: atomic\n---\n# x\n",
        encoding="utf-8",
    )
    await pool.skill_registry.discover()

    await engine.submit(RefreshSnapshot())
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.01)
        if engine.snapshot.version > v0:
            break
    assert engine.snapshot.version > v0
    assert engine.snapshot.get("extra-skill") is not None
    await pool.close()
