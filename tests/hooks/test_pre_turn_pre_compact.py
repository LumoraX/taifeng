"""hook-wiring-pre-compact-pre-turn 集成测试。

覆盖 spec ``hooks`` 新增的两条 Requirement：
    - pre_turn hook 调用点：allow → 正常；deny → emit pre_turn_hook_denied + turn_failed
    - pre_compact hook 调用点：allow → 走 strategy；deny → emit pre_compact_hook_skipped

测试通过 EnginePool 注入 HookRunner，端到端验证事件流。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import CompactNow


# --------------------------------------------------------------------
# 公共辅助
# --------------------------------------------------------------------


async def _drain_until(
    engine: taifeng.AgentEngine,
    submission_id: str,
    stop_kinds: tuple[str, ...],
    timeout: float = 5.0,
) -> list[str]:
    """订阅 engine.subscribe(submission_id)，收集事件 kind 直到见到 stop_kinds 之一。"""
    seen: list[str] = []

    async def _collect() -> None:
        async for ev in engine.subscribe(submission_id):
            seen.append(ev.msg.kind)
            if ev.msg.kind in stop_kinds:
                return

    await asyncio.wait_for(_collect(), timeout=timeout)
    return seen


async def _start_collector(
    engine: taifeng.AgentEngine,
) -> tuple[list[str], asyncio.Task]:
    """启动 subscribe_all 收集器；必须在 submit 之前调用以避免漏事件。

    返回 (seen_list, task)。调用方稍后 cancel task 即可停止收集。
    """
    seen: list[str] = []
    ready = asyncio.Event()

    async def _collect() -> None:
        # subscribe_all 是 async generator；首次 await q.get() 之前会注册到 _all_subs
        gen = engine.subscribe_all()
        # 通过 __anext__ 触发 generator 推进到 q.append 之后
        # 用一个 done flag 让外层知道已经注册
        async for ev in gen:
            if not ready.is_set():
                ready.set()  # 这里其实已晚一拍，但 ready 仅作 best-effort 信号
            seen.append(ev.msg.kind)

    task = asyncio.create_task(_collect())
    # 让 _collect 任务执行到 subscribe_all 注册 queue 那一步
    # （subscribe_all 在 yield 之前会先 q.put_nowait，且 q 已 append 到 _all_subs）
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return seen, task


async def _stop_collector(
    seen: list[str], task: asyncio.Task, settle_seconds: float = 0.3,
) -> list[str]:
    await asyncio.sleep(settle_seconds)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass
    return list(seen)


# --------------------------------------------------------------------
# 1) pre_turn hook —— allow 路径
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_turn_hook_allow_passes_through(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """注册 always-allow 的 pre_turn hook → turn 正常完成。"""
    client = SimClient(turns=[SimTurn(
        text="ok",
        usage=TokenUsage(input_tokens=10, output_tokens=2),
    )])
    reg = HookRegistry()
    call_log: list[str] = []

    async def allow_handler(hook, ctx) -> HookDecision:
        call_log.append(f"pre_turn:{hook.iteration}:{hook.user_text[:20]}")
        return HookDecision.ok()

    reg.register("pre_turn", allow_handler)
    runner = HookRunner(reg)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=runner,
    )
    engine = await pool.get_or_create(
        session_id="s-allow", entry_skill_id="code-reviewer",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="问个简单问题"))
    seen = await _drain_until(
        engine, sub_id, stop_kinds=("turn_completed", "turn_failed"),
    )
    await pool.close()

    assert call_log == ["pre_turn:0:问个简单问题"], call_log
    assert "turn_started" in seen
    assert "turn_completed" in seen
    assert "pre_turn_hook_denied" not in seen
    assert "turn_failed" not in seen


# --------------------------------------------------------------------
# 2) pre_turn hook —— deny 路径
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_turn_hook_deny_blocks_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """注册 always-deny 的 pre_turn hook → emit pre_turn_hook_denied + turn_failed。

    验证：
        - 不出现 turn_started
        - user_message 仍持久化（resume 友好）
        - _turn_index 仍 +1
    """
    client = SimClient(turns=[])  # turn 不会真正跑，不需要 LLM 响应
    reg = HookRegistry()

    async def deny_handler(hook, ctx) -> HookDecision:
        return HookDecision.deny("quota_exceeded")

    reg.register("pre_turn", deny_handler)
    runner = HookRunner(reg)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[], hooks=runner,
    )
    engine = await pool.get_or_create(
        session_id="s-deny", entry_skill_id="code-reviewer",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="被拒的请求"))
    seen = await _drain_until(
        engine, sub_id, stop_kinds=("turn_completed", "turn_failed"),
    )

    # === 断言事件流 ===
    assert "pre_turn_hook_denied" in seen
    assert "turn_failed" in seen
    assert "turn_started" not in seen
    # 顺序：denied 在 failed 之前
    assert seen.index("pre_turn_hook_denied") < seen.index("turn_failed")

    # === 断言 user_message 仍持久化 ===
    tid = engine.thread_id
    gen = await pool.store.load_thread(tid)
    items = [it async for it in gen]
    user_msgs = [it for it in items if it.kind == "user_message"]
    assert any(it.payload.get("text") == "被拒的请求" for it in user_msgs)

    # === 断言 turn_index 单调 +1 ===
    assert engine._turn_index == 1  # noqa: SLF001

    await pool.close()


# --------------------------------------------------------------------
# 3) pre_compact hook —— allow 路径（manual force=True）
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_compact_hook_allow_runs_compaction(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """注册 always-allow 的 pre_compact hook + 主动 CompactNow → 走 strategy 路径。

    用 force=True 绕过阈值，强制 hook 被调用。
    """
    client = SimClient(turns=[])
    reg = HookRegistry()
    pre_compact_seen: list[str] = []

    async def allow_handler(hook, ctx) -> HookDecision:
        pre_compact_seen.append(f"{hook.phase}:{hook.history_length}")
        return HookDecision.ok()

    reg.register("pre_compact", allow_handler)
    runner = HookRunner(reg)

    # 提供 compressors（注意：pool.create compressors=None 时会默认装 Handoff+Sliding；
    # 这里给 empty list 也会被 pool 内部"or"成默认 —— 看 pool 实现）。
    # 实际：compressors=[] 时 `if compressors else None` 走 None 分支不自动填默认，
    # 所以 _maybe_compress 会因 compressors is None 提前 return（hook 不被调用）。
    # 因此本测试不传 compressors（让 pool.create 装默认）。
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, hooks=runner,
    )
    engine = await pool.get_or_create(
        session_id="s-allow-compact", entry_skill_id="code-reviewer",
    )
    seen_buf, collector_task = await _start_collector(engine)
    await engine.submit(CompactNow(force=True))
    seen = await _stop_collector(seen_buf, collector_task, settle_seconds=0.3)
    await pool.close()

    # hook 被调用了 1 次，phase=manual
    assert len(pre_compact_seen) == 1, pre_compact_seen
    assert pre_compact_seen[0].startswith("manual:"), pre_compact_seen
    # 走到了 CompactionStarted（hook 允许后的下一步）
    assert "compaction_started" in seen, seen
    assert "pre_compact_hook_skipped" not in seen


# --------------------------------------------------------------------
# 4) pre_compact hook —— deny 路径
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_compact_hook_deny_skips_compaction(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """注册 always-deny 的 pre_compact hook + 主动 CompactNow → 跳过压缩。

    验证：
        - emit pre_compact_hook_skipped（含 phase / reason）
        - 不 emit compaction_started / compaction_completed
        - history_buffer 长度不变
    """
    client = SimClient(turns=[])
    reg = HookRegistry()

    async def deny_handler(hook, ctx) -> HookDecision:
        return HookDecision.deny("tenant_disabled")

    reg.register("pre_compact", deny_handler)
    runner = HookRunner(reg)

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, hooks=runner,
    )
    engine = await pool.get_or_create(
        session_id="s-deny-compact", entry_skill_id="code-reviewer",
    )
    history_len_before = len(engine.history_snapshot())

    seen_buf, collector_task = await _start_collector(engine)
    await engine.submit(CompactNow(force=True))
    seen = await _stop_collector(seen_buf, collector_task, settle_seconds=0.3)
    await pool.close()

    assert "pre_compact_hook_skipped" in seen, seen
    assert "compaction_started" not in seen
    assert "compaction_completed" not in seen
    # 历史长度不变（压缩被跳过）
    history_len_after = len(engine.history_snapshot())
    assert history_len_after == history_len_before
