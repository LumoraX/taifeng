"""K6：/proc 式自省 —— engine.introspect() + pool.introspect()。"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage


@pytest.mark.asyncio
async def test_engine_introspect_shape_and_updates(
    skills_dir: Path, threads_dir: Path
) -> None:
    client = MockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=30, total_tokens=30)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
        max_concurrent_spawns=8, max_total_spawns=100,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    snap0 = engine.introspect()
    # 形状：关键 /proc 字段齐全
    for key in (
        "thread_id", "entry_skill_id", "running", "pending_submissions",
        "pending", "turn_index", "spawn", "session_tokens", "events_dropped",
        "context_tokens", "context_window", "cache",
    ):
        assert key in snap0, key
    assert snap0["spawn"]["max_concurrent"] == 8
    assert snap0["session_tokens"] == 0
    assert snap0["pending_submissions"] == []
    assert snap0["pending"] == []

    sub = await engine.submit(taifeng.UserMessage(text="hi"))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    snap1 = engine.introspect()
    # 跑完一轮：累计 token 增长、turn_index 推进、无残留在飞
    assert snap1["session_tokens"] >= 30
    assert snap1["turn_index"] >= 1
    assert snap1["pending_submissions"] == []

    await pool.close()


@pytest.mark.asyncio
async def test_introspect_pending_exposes_cancel_state(
    skills_dir: Path, threads_dir: Path
) -> None:
    """`pending` 逐条暴露在飞 turn 的取消态（cancel_requested）。

    白盒：直接往 `_pending` 注入一个已取消的 _PendingTurn，验证投影逻辑——
    这是参考实现 lane_board 在内核侧可纯读暴露的那一半（staleness 阈值留宿主）。
    """
    from taifeng.loop.engine import _PendingTurn

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=MockClient(turns=[MockTurn(text="ok")]), compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")

    # 注入两个在飞 turn：一个未取消、一个已请求取消
    tok_live = taifeng.CancellationToken(name="sub:live")
    tok_killed = taifeng.CancellationToken(name="sub:killed")
    tok_killed.cancel()
    engine._pending["live"] = _PendingTurn(submission_id="live", cancel=tok_live)
    engine._pending["killed"] = _PendingTurn(submission_id="killed", cancel=tok_killed)

    snap = engine.introspect()
    by_id = {p["submission_id"]: p["cancel_requested"] for p in snap["pending"]}
    assert by_id == {"live": False, "killed": True}
    # 纯 ID 视图保持向后兼容
    assert set(snap["pending_submissions"]) == {"live", "killed"}

    engine._pending.clear()
    await pool.close()


@pytest.mark.asyncio
async def test_pool_introspect_lists_active_engines(
    skills_dir: Path, threads_dir: Path
) -> None:
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=MockClient(turns=[MockTurn(text="ok")]), compressors=[],
    )
    await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")

    view = pool.introspect()
    assert set(view.keys()) == {"s1", "s2"}
    assert view["s1"]["entry_skill_id"] == "code-reviewer"
    assert "spawn" in view["s2"]

    await pool.close()
