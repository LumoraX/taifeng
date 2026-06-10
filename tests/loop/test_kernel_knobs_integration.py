"""内核旋钮公开 API 集成测试 —— K1–K4 + K6 经 `EnginePool.create(...)` 一条流程接出并实跑。

各 K 机制都有专测（test_spawn_registry / test_resource_limit / test_memory_swap /
test_bus_backpressure / test_introspect），本文补的是**业务真正接触的装配面**：
把所有内核旋钮经公开构造器一次性接出，跑真 turn，验证它们协同可用（回归守卫，
防止某次重构悄悄把某个 kwarg 吞掉 / 不再透传到 TurnRunner）。

对标 `docs/configurable-knobs.md §1.0`（内核资源/内存旋钮）+ `/examples/kernel_knobs/`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage


class _SpyMemory:
    """K3：最小 MemoryStore —— 记录每个换页钩子是否被内核回调。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        self.calls.append("prefetch")
        return "「长期记忆」既往要点。"  # 换入注入 prompt 尾部（cache-aware）

    async def writeback(self, *, thread_id: str, items: object) -> None:
        self.calls.append("writeback")

    async def on_pre_evict(self, items: object) -> str:
        self.calls.append("on_pre_evict")
        return ""

    async def on_session_end(self, *, thread_id: str, items: object) -> None:
        self.calls.append("on_session_end")


@pytest.mark.asyncio
async def test_all_kernel_knobs_wired_through_public_api(
    skills_dir: Path, threads_dir: Path
) -> None:
    """K1/K3/K4 旋钮全经 EnginePool.create 接出 + 真跑一轮 tool-calling turn。

    断言：spawn 配额按注入值落进 introspect；session token 计数实时累计；
    K3 三个钩子（prefetch / writeback / on_session_end）都被回调；K4 计数到位。
    """
    mem = _SpyMemory()
    client = SimClient(turns=[
        SimTurn(
            text="加载规则…",
            tool_calls=[{"id": "tc1", "name": "read_skill",
                         "arguments": '{"skill_id": "style-checker"}'}],
            usage=TokenUsage(input_tokens=200, output_tokens=30, total_tokens=230),
        ),
        SimTurn(
            text="函数 120>80，建议拆分。",
            usage=TokenUsage(input_tokens=380, output_tokens=55, total_tokens=435,
                             cache_read_input_tokens=190),
        ),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
        # —— K1–K4 内核旋钮（照 configurable-knobs.md §1.0 一次性接出）——
        max_concurrent_spawns=8,    # K1 广度准入
        max_total_spawns=100,       # K1 兜底
        memory_store=mem,           # K3 内存层级
        submission_queue_size=64,   # K4 入站流控
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub = await engine.submit(taifeng.UserMessage(text="审查 def getCwd(x): ..."))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break

    snap = engine.introspect()
    # K1：注入的配额值落进 /proc 快照
    assert snap["spawn"]["max_concurrent"] == 8
    assert snap["spawn"]["max_total"] == 100
    # K2：token 计数实时累计（即便未设上限）
    assert snap["session_tokens"] > 0
    # K4：出站丢弃计数在位（慢消费者自检）
    assert snap["events_dropped"] == 0
    # K6：无残留在飞 + 新增逐条视图存在
    assert snap["pending"] == []

    await pool.close()  # 触发 K3 on_session_end

    # K3：三个换页钩子都被内核回调（page-in / dirty-page / teardown）
    assert "prefetch" in mem.calls
    assert "writeback" in mem.calls
    assert "on_session_end" in mem.calls


@pytest.mark.asyncio
async def test_session_token_oom_refuses_next_turn(
    skills_dir: Path, threads_dir: Path
) -> None:
    """K2 OOM-killer：max_session_tokens 触顶后，下一轮被 pre-turn 守卫拒绝。

    第一轮真实消耗 5000 token（> 1000 上限）→ 会话累计触顶；第二轮入队时
    pre-turn 守卫应拒绝并 emit resource_limit_exceeded（而非静默继续）。
    """
    client = SimClient(turns=[
        SimTurn(text="第一轮", usage=TokenUsage(input_tokens=5000, total_tokens=5000)),
        SimTurn(text="第二轮", usage=TokenUsage(input_tokens=5000, total_tokens=5000)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=client, compressors=[],
        max_session_tokens=1000,    # K2：1k 上限，第一轮就触顶
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")

    kinds: list[str] = []
    sub1 = await engine.submit(taifeng.UserMessage(text="第一条"))
    async for ev in engine.subscribe(sub1):
        kinds.append(ev.msg.kind)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    sub2 = await engine.submit(taifeng.UserMessage(text="第二条"))
    async for ev in engine.subscribe(sub2):
        kinds.append(ev.msg.kind)
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_refused",
                           "resource_limit_exceeded"):
            break

    # 触顶后第二轮必须被内核拒绝，不能静默放行
    assert any(k in ("turn_refused", "resource_limit_exceeded") for k in kinds), kinds

    await pool.close()
