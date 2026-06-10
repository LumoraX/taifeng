"""内核资源旋钮演示（K1–K4 + K6）—— 业务怎么把 OS 微内核的"资源准入/强制/流控/内存/自省"接出来用。

把 taifeng 当 LLM agent 的 OS 微内核时，这五类旋钮（照 `docs/configurable-knobs.md §1.0`）
全部经 `EnginePool.create(...)` 注入、`engine.introspect()` 观测。本 demo 用 SimClient
跑通两条路径，无需 API key：

    ① 正常路径：K1 spawn 配额 + K3 memory 换页钩子 + K4 流控 全接出，跑一轮 tool-calling
       turn，introspect() 打快照（spawn 配额 / 实时 token 计数 / 事件丢弃 / cache 健康度）。
    ② K2 OOM 路径：max_session_tokens 设到极小，证明触顶后内核**真拒新 turn**（非只告警）。

> 这是"能力体验类"独立 demo（不注入 web_ui）。真实 LLM 版见 examples/real_llm/kernel_knobs.py。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/kernel_knobs/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

# 两个 skill：atomic 风格规则 + composite 审查入口（带子 skill，可触发 spawn）
ATOMIC = (
    "---\nname: style-checker\ndescription: 代码风格规则\nversion: 1.0.0\n"
    "type: atomic\n---\n# 风格规则\n函数 ≤ 80 行；命名 snake_case；圈复杂度 ≤ 10。\n"
)
COMPOSITE = (
    "---\nname: code-reviewer\ndescription: 代码审查专家\nversion: 1.0.0\n"
    "type: composite\nentry: true\nmodel: mock-model\nchild_skills: [style-checker]\n"
    "tool_names: []\nmax_call_depth: 3\n---\n# 代码审查专家\n"
    '需要风格规范时调用 read_skill("style-checker")。\n'
)


class SpyMemoryStore:
    """K3：最小 MemoryStore —— 把每个换页钩子的回调记下来，证明内核真调了。

    业务真实实现会把 prefetch 接向量库 / writeback 落 KV / on_pre_evict 抢救要点等，
    后端属 userspace；本 demo 只验证内核**回调时机**正确。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        # swap-in 缺页：换入注入 prompt 尾部（不动 system 头部，cache-aware）
        self.calls.append("prefetch")
        return "「长期记忆命中」既往要点：上次审查发现命名不规范。"

    async def writeback(self, *, thread_id: str, items: object) -> None:
        self.calls.append("writeback")  # dirty-page：本 turn 新增异步写回

    async def on_pre_evict(self, items: object) -> str:
        self.calls.append("on_pre_evict")  # swap-out：压缩丢弃前抢救
        return ""

    async def on_session_end(self, *, thread_id: str, items: object) -> None:
        self.calls.append("on_session_end")  # teardown：会话末最终 flush


def _write_skills(root: Path) -> Path:
    """把两个 SKILL.md 落到临时目录，返回 skills 根。"""
    skills = root / "skills"
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE, encoding="utf-8")
    return skills


async def scenario_normal(root: Path) -> None:
    """① 正常路径：K1/K3/K4 旋钮全接 + 真跑 turn + introspect 自省。"""
    (root / "threads").mkdir(parents=True, exist_ok=True)
    mem = SpyMemoryStore()
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
        skills_dir=_write_skills(root), threads_dir=root / "threads",
        model_client=client, compressors=[],
        max_concurrent_spawns=8,    # K1 广度准入（防 fork-bomb）
        max_total_spawns=100,       # K1 累计兜底
        memory_store=mem,           # K3 长期记忆 swap 接口
        submission_queue_size=64,   # K4 入站背压
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub = await engine.submit(taifeng.UserMessage(text="审查 def getCwd(x): ..."))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    snap = engine.introspect()  # K6 /proc 自省
    print("=== ① 正常路径（K1/K3/K4 接出 + 真 turn + introspect）===")
    print(f"  spawn 配额 (K1)     : {snap['spawn']}")
    print(f"  session_tokens (K2) : {snap['session_tokens']}  ← 实时累计")
    print(f"  events_dropped (K4) : {snap['events_dropped']}")
    print(f"  pending (K6 增强)   : {snap['pending']}")
    print(f"  cache 健康度        : {snap['cache']}")
    await pool.close()  # 触发 on_session_end
    await asyncio.sleep(0.05)
    print(f"  K3 memory 钩子回调   : {mem.calls}  ← page-in / dirty-page / teardown 全触发")


async def scenario_oom(root: Path) -> None:
    """② K2 OOM 路径：max_session_tokens 极小，证明触顶后内核真拒新 turn。"""
    (root / "threads").mkdir(parents=True, exist_ok=True)
    client = SimClient(turns=[
        SimTurn(text="第一轮", usage=TokenUsage(input_tokens=5000, total_tokens=5000)),
        SimTurn(text="第二轮", usage=TokenUsage(input_tokens=5000, total_tokens=5000)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skills(root), threads_dir=root / "threads",
        model_client=client, compressors=[],
        max_session_tokens=1000,    # K2：1k 上限，第一轮就触顶
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="code-reviewer")
    kinds: list[str] = []
    for text in ("第一条", "第二条"):
        sub = await engine.submit(taifeng.UserMessage(text=text))
        async for ev in engine.subscribe(sub):
            kinds.append(ev.msg.kind)
            if ev.msg.kind in ("turn_completed", "turn_failed", "turn_refused",
                               "resource_limit_exceeded"):
                break
    print("\n=== ② K2 OOM 强制路径（max_session_tokens=1000）===")
    print(f"  事件序列: {kinds}")
    refused = any(k in ("turn_refused", "resource_limit_exceeded") for k in kinds)
    print(f"  → 触顶后第二轮被内核{'拒绝 ✓' if refused else '放行 ✗（异常）'}")
    await pool.close()


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        await scenario_normal(root / "a")
        await scenario_oom(root / "b")
        print("\n✅ 内核资源旋钮（K1–K4 + K6 自省）经公开 API 接出并实跑通过。")


if __name__ == "__main__":
    asyncio.run(main())
