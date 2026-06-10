"""pinned 状态注入面 e2e:EnginePool/AgentEngine 参数透传 + 运行时增删 + R5 resume。

覆盖 tasks 3.2 / 4.2:
  - 构造期 ``pinned_state_sources`` → CompactNow 后 pinned 项进 history;
  - 运行时 ``register_pinned_state`` / ``unregister_pinned_state`` 生效于下一次压缩;
  - R5:重注入项经 store 持久化,resume 重载历史含 pinned 项;
  - K3 叠加:memory salvage digest 与 pinned 项共存(双钩子正交)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import taifeng
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.submission import CompactNow

if TYPE_CHECKING:
    from pathlib import Path


class _PlanSrc:
    """测试用 pinned source:固定渲染一段规划文本。"""

    name = "plan"
    max_chars = 500

    def __init__(self, text: str = "规划:A 已完成,继续 B") -> None:
        self._text = text

    def format_for_injection(self) -> str:
        return self._text


def _handoff_compressor() -> list:
    summary_client = SimClient(turns=[
        SimTurn(text="## 摘要", usage=TokenUsage(input_tokens=100, output_tokens=10))
        for _ in range(4)
    ])
    return [HandoffCompactionStrategy(model_client=summary_client)]


async def _drive(engine, texts: list[str]) -> None:
    """逐条提交并等终态。"""
    for t in texts:
        sub_id = await engine.submit(taifeng.UserMessage(text=t))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break


async def _compact(engine) -> list:
    """CompactNow(force) 并收集本次 submission 的事件。"""
    msgs: list = []
    sub_id = await engine.submit(CompactNow(force=True))
    async for ev in engine.subscribe(sub_id):
        msgs.append(ev.msg)
        if ev.msg.kind in ("compaction_completed", "turn_failed"):
            break
    return msgs


def _pinned_items(engine) -> list:
    return [it for it in engine.history_snapshot()
            if it.kind == "system_injection"
            and str(it.payload.get("source", "")).startswith("pinned:")]


async def test_ctor_sources_reinjected_after_compact_now(
    skills_dir: Path, threads_dir: Path
) -> None:
    """构造期 pinned_state_sources → 手动压缩后 pinned 项钉回 history 尾。"""
    client = SimClient(turns=[SimTurn(text=f"r{i}") for i in range(8)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=_handoff_compressor(),
        pinned_state_sources=[_PlanSrc()],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    await _drive(engine, ["a", "b", "c"])
    msgs = await _compact(engine)

    assert any(m.kind == "pinned_state_reinjected" for m in msgs)
    items = _pinned_items(engine)
    assert len(items) == 1
    assert items[0].payload["source"] == "pinned:plan"
    assert "继续 B" in items[0].payload["text"]
    await pool.close()


async def test_runtime_register_unregister(
    skills_dir: Path, threads_dir: Path
) -> None:
    """运行时 register → 下一次压缩生效;unregister → 再压缩不再注入。

    每次 compact 前补足历史(handoff 要求 len > preserve_tail+1,默认 4),
    断言走事件级(pinned_state_reinjected 是否出现)——旧 pinned 项作为普通
    历史可能被后续压缩吸收,条目计数不是稳定信号。
    """
    client = SimClient(turns=[SimTurn(text=f"r{i}") for i in range(12)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=_handoff_compressor(),
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")

    # 未注册 → 压缩成功但无 pinned 事件
    await _drive(engine, ["a", "b", "c"])
    msgs1 = await _compact(engine)
    done1 = next(m for m in msgs1 if m.kind == "compaction_completed")
    assert done1.data["success"] is True
    assert not any(m.kind == "pinned_state_reinjected" for m in msgs1)
    assert _pinned_items(engine) == []

    # register → 下一次压缩注入
    await _drive(engine, ["d", "e", "f"])
    engine.register_pinned_state(_PlanSrc())
    msgs2 = await _compact(engine)
    assert any(m.kind == "pinned_state_reinjected" for m in msgs2)
    assert len(_pinned_items(engine)) == 1

    # unregister → 再压缩不再注入(事件不出现)
    await _drive(engine, ["g", "h", "i"])
    engine.unregister_pinned_state("plan")
    msgs3 = await _compact(engine)
    done3 = next(m for m in msgs3 if m.kind == "compaction_completed")
    assert done3.data["success"] is True
    assert not any(m.kind == "pinned_state_reinjected" for m in msgs3)
    await pool.close()


async def test_pinned_item_survives_resume(
    skills_dir: Path, threads_dir: Path
) -> None:
    """R5:pinned 项经 store 持久化;新 pool resume 同 thread 后历史仍含该项。"""
    client = SimClient(turns=[SimTurn(text=f"r{i}") for i in range(8)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=_handoff_compressor(),
        pinned_state_sources=[_PlanSrc("跨进程保活验证文本")],
    )
    engine = await pool.get_or_create(session_id="s3", entry_skill_id="code-reviewer")
    await _drive(engine, ["a", "b", "c"])
    await _compact(engine)
    thread_id = engine.thread_id
    assert _pinned_items(engine)
    await pool.close()

    pool2 = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=[SimTurn(text="ok")]),
        compressors=_handoff_compressor(),
    )
    engine2 = await pool2.get_or_create(
        session_id="s3-resumed", entry_skill_id="code-reviewer",
        resume_thread_id=thread_id,
    )
    items = _pinned_items(engine2)
    assert items, "resume 后历史丢失 pinned 项(R5 破坏)"
    assert "跨进程保活验证文本" in items[0].payload["text"]
    await pool2.close()


async def test_pinned_coexists_with_memory_salvage(
    skills_dir: Path, threads_dir: Path
) -> None:
    """K3 叠加:memory on_pre_evict digest 与 pinned 项同次压缩各自就位(正交)。

    走 pre_turn 自然触发(小 context_window):manual CompactNow 路径既有设计
    不带 memory_store,K3 salvage 只在 turn 内压缩接缝生效。
    """
    from taifeng.context.budget import ContextBudget

    class _Mem:
        """最小 MemoryStore:salvage 返回固定 digest。"""

        async def prefetch(self, query: str, *, thread_id: str) -> str:
            return ""

        async def writeback(self, *, thread_id: str, items: list) -> None:
            return None

        async def on_pre_evict(self, evicted: list) -> str:
            return "digest:被换出内容摘要"

        async def on_session_end(self, *, thread_id: str) -> None:
            return None

    client = SimClient(turns=[SimTurn(text=f"r{i}") for i in range(8)])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=_handoff_compressor(),
        budget=ContextBudget(context_window=500),
        pinned_state_sources=[_PlanSrc()],
        memory_store=_Mem(),
    )
    engine = await pool.get_or_create(session_id="s4", entry_skill_id="code-reviewer")
    # 长文本驱动多轮,超 soft limit 触发 pre_turn 压缩(K3 salvage 路径)
    await _drive(engine, [f"轮{i}:" + "内容" * 600 for i in range(5)])

    history = engine.history_snapshot()
    sources = [it.payload.get("source") for it in history
               if it.kind == "system_injection"]
    assert "memory_pre_evict" in sources
    assert "pinned:plan" in sources
    # 顺序:salvage digest 在 summary 之后、pinned 在尾部
    assert sources.index("memory_pre_evict") < sources.index("pinned:plan")
    await pool.close()
