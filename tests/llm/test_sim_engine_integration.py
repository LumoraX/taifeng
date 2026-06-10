"""sim × engine 链路集成验证 —— 用 conformance 模拟器打最近漏网面。

四个场景（task 5.1）：
    ① resume 重建：跨 pool 续接后前缀一致 → cache_read > 0，且 call_id 配对
      经合同校验零违规；
    ② rollback 截断：截掉最近一轮后再采样，账本如实反映前缀仍命中、零违规；
    ③ 小窗 overflow → 自愈闭环：有压缩器一次自愈成功；
      「压缩了个寂寞」（无有效压缩）→ 二次 overflow 硬失败 turn_failed；
    ④ 并发 spawn + SimCoordinator 显式编排完成顺序（join 语义的确定性基础）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import taifeng
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.llm.providers import RoutingSimClient, SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import ThreadRollback


async def _run_turn(engine: taifeng.AgentEngine, text: str) -> str:
    """提交一轮并等终态，返回 turn_completed / turn_failed。"""
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            return ev.msg.kind
    raise AssertionError("未收到终态事件")


async def _wait(cond, tries: int = 200) -> bool:
    """轮询等待条件成立（spawn 后台 task 用）。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_resume_rebuild_prefix_consistent_and_callid_paired(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """① resume 重建：同一 client 跨 pool 续接 → 前缀命中 cache_read>0 + 零违规。

    第一轮含真实 tool call（read_skill），resume 重建必须完整带回
    function_call/function_call_output 配对——配对错位会被合同校验当场抓红。
    """
    client = SimClient(turns=[
        SimTurn(text="先看子技能", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
        ]),
        SimTurn(text="第一轮结论", usage=TokenUsage(input_tokens=100, output_tokens=10)),
        SimTurn(text="续接轮结论"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    thread_id = engine.thread_id
    assert await _run_turn(engine, "第一轮的问题") == "turn_completed"
    await pool.close()

    # 同一 client 接到新 pool（provider 前缀缓存视角跨实例存续）
    pool2 = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine2 = await pool2.get_or_create(
        session_id="s1-resume", entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    assert await _run_turn(engine2, "续接的问题") == "turn_completed"
    await pool2.close()

    # 重建无漂移：续接采样与挂起前请求共享长前缀
    assert client.server.last_cache_read > 0
    # call_id 配对 + 全部合同规则零违规（资深断言：重放/错位/复读当场红）
    assert client.ledger.violations == []
    # 续接请求里必须带回第一轮的工具结果（闭环验证工具结果真的回传了）
    assert client.ledger.saw_function_call("c1")
    assert client.ledger.function_call_output_text("c1") is not None


@pytest.mark.asyncio
async def test_rollback_truncation_reflected_in_ledger(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """② rollback 截断最近一轮后再采样：零违规 + 前缀账本如实反映仍命中。"""
    client = SimClient(turns=[
        SimTurn(text="第一轮答"),
        SimTurn(text="第二轮答"),
        SimTurn(text="回滚后的重答"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    assert await _run_turn(engine, "第一问") == "turn_completed"
    assert await _run_turn(engine, "第二问") == "turn_completed"

    # 回滚最近 1 轮（截掉第二问及其答复）
    await engine.submit(ThreadRollback(turns=1))
    await asyncio.sleep(0.05)  # rollback 是 turn 间操作，无终态事件可等

    assert await _run_turn(engine, "回滚后的新问") == "turn_completed"
    await pool.close()

    # 截断不是漂移：与第一轮共享前缀 → 仍命中；且全程零合同违规
    assert client.server.last_cache_read > 0
    assert client.ledger.violations == []
    # 回滚后的请求里不得再出现被截掉的第二问
    assert "第二问" not in client.ledger.requests()[-1].blob()


@pytest.mark.asyncio
async def test_overflow_self_recovery_with_compressor(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """③a 小窗 overflow → provider_retry → 强制压缩 → 重采样成功（自愈闭环）。"""
    client = SimClient(
        turns=[
            SimTurn(text="塞大上下文的答复" * 300),  # 2400 字符 ≈ 600 tokens，撑大历史
            SimTurn(text="第二轮答"),                # 触发 overflow 后重采样成功的剧本
        ],
        context_window=600,
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client,
        compressors=[SlidingWindowStrategy(keep_tail=1)],
    )
    engine = await pool.get_or_create(session_id="s3", entry_skill_id="code-reviewer")
    assert await _run_turn(engine, "第一问" * 30) == "turn_completed"
    # 第二轮请求估算超窗 → ContextOverflowError → 强制 sliding 压缩 → 重采样成功
    assert await _run_turn(engine, "第二问") == "turn_completed"
    await pool.close()
    assert client.ledger.violations == []
    # 自愈消耗了重采样：两轮共发生 3 次采样（第二轮 overflow 那次未产出事件但已记录）
    assert len(client.ledger.requests()) == 3


@pytest.mark.asyncio
async def test_overflow_without_effective_compressor_fails_loud(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """③b「压缩了个寂寞」：无有效压缩 → 二次 overflow → turn_failed（不静默）。"""
    client = SimClient(
        turns=[SimTurn(text="塞大上下文的答复" * 300), SimTurn(text="不该被消费")],
        context_window=600,
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],  # 没有任何压缩策略 → 自愈压缩无效果
    )
    engine = await pool.get_or_create(session_id="s4", entry_skill_id="code-reviewer")
    assert await _run_turn(engine, "第一问" * 30) == "turn_completed"
    assert await _run_turn(engine, "第二问") == "turn_failed"
    await pool.close()
    assert client.ledger.violations == []


@pytest.mark.asyncio
async def test_concurrent_spawn_orchestrated_completion_order(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """④ 并发 spawn：SimCoordinator 编排「B 必先于 A 完成」，事件序确定可断言。"""
    client = RoutingSimClient(routes={
        # style-checker 被 spawn 两次：第 1 实例等信号（A）、第 2 实例点信号（B）
        "style-checker": [
            SimTurn(text="A-结论", await_signal="b-done"),
            SimTurn(text="B-结论", emit_signal="b-done"),
        ],
        "code-reviewer": [SimTurn(text="主控")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s5", entry_skill_id="code-reviewer")

    completed_order: list[str] = []

    async def watch() -> None:
        """收集 spawn_completed 顺序，两条即止。"""
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "spawn_completed":
                completed_order.append(ev.msg.data["handle_id"])
                if len(completed_order) >= 2:
                    return

    task = asyncio.create_task(watch())
    h_a = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="A"))["handle_id"]
    h_b = (await engine.spawn_skill(skill_id="style-checker", args={}, reason="B"))["handle_id"]

    assert await _wait(lambda: all(
        engine.spawn_status([h])[h]["status"] == "done" for h in (h_a, h_b)
    ))
    await asyncio.wait_for(task, timeout=2.0)
    await pool.close()

    # 确定性时序：A 等 B 的信号 → B 必先完成
    assert completed_order == [h_b, h_a]
    assert engine.spawn_status([h_a])[h_a]["result"] == "A-结论"
    assert engine.spawn_status([h_b])[h_b]["result"] == "B-结论"
    assert client.ledger.violations == []
