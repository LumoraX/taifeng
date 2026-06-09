"""上下文压缩 demo —— 本地 budget 到达上限即主动压缩（sliding 兜底，MockClient 无需 key）。

演示「机制1：本地 budget 主动压缩」——配极小 `context_window`（1024，正常 LLM 是
128k–1M），连续多轮把 history 撑过 soft/hard 上限，taifeng **不依赖 provider 报错**、
在 turn 起点主动触发 `SlidingWindowStrategy` 压缩。时间轴会出现 `compaction_started` /
`compaction_completed`（phase=pre_turn），history 中段被丢弃 + 写入滑窗 placeholder。

真实 LLM 版（handoff 摘要）：examples/real_llm/capability_matrix.py 的 compression 场景。
契约：docs/architecture/context-compression.md。

运行：
    PYTHONPATH=src uv run python examples/compression_showcase/demo.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.telemetry import attach_console_sink

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# 一段足够长的 mock 回复（健谈助手风格）——让 history 每轮快速累积，越过极小 window。
_LONG_REPLY = (
    "## 背景\n这是一段刻意写长的演示回复，用来快速撑大对话历史。"
    "Python 装饰器本质是接收函数返回函数的高阶函数，常用于横切关注点。\n"
    "## 举例\n@staticmethod / @property / @functools.lru_cache 都是装饰器；"
    "它们在不改动原函数体的前提下增强行为。\n"
    "## 对比\n类装饰器 vs 函数装饰器：前者可持有状态，后者更轻量。\n"
    "## 延伸阅读\nPEP 318 引入装饰器语法；functools.wraps 用于保留元信息。"
) * 6  # 放大到 ~1700 字符/轮，几轮即越过 hard 上限（char/4 估算 > 973 token）


def _chatty_client(rounds: int) -> MockClient:
    """每轮回放一段长文本（无 tool call，一轮一次采样）。"""
    return MockClient(
        turns=[
            MockTurn(text=_LONG_REPLY, usage=TokenUsage(input_tokens=200, output_tokens=180))
            for _ in range(rounds)
        ]
    )


async def main() -> None:
    """连续多轮提问，观察本地 budget 到顶后主动 sliding 压缩。"""
    import tempfile

    rounds = 6
    with tempfile.TemporaryDirectory() as td:
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=Path(td) / "threads",
            model_client=_chatty_client(rounds),
            # 极小 window + 滑窗兜底压缩器：正常 128k–1M，这里 1024 逼出压缩
            budget=ContextBudget(context_window=1024, soft_limit_ratio=0.85, hard_limit_ratio=0.95),
            compressors=[SlidingWindowStrategy(keep_tail=2)],
        )
        engine = await pool.get_or_create(
            session_id="demo-compression", entry_skill_id="chatty-assistant"
        )
        sink_task = attach_console_sink(engine, color=True)

        events: list = []
        forward = asyncio.create_task(_collect(engine, events))

        for i in range(rounds):
            sub_id = await engine.submit(
                taifeng.UserMessage(text=f"第{i + 1}问：请详细讲讲 Python 装饰器，越详细越好。")
            )
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break

        await asyncio.sleep(0.5)
        await pool.close()
        await asyncio.sleep(0.2)
        sink_task.cancel()
        forward.cancel()

        # ── 结论 ──
        comp = [m for m in events if m.kind == "compaction_started"]
        done = [m for m in events if m.kind == "compaction_completed"]
        phases = [m.data.get("phase") for m in comp]
        print("\n" + "=" * 60)
        print(f"compaction_started 次数 = {len(comp)}  phases={phases}")
        print(f"compaction_completed     = {[m.data.get('success') for m in done]}")
        ok = len(comp) > 0
        print(f"==> 本地 budget 到顶主动压缩{'确证 ✅' if ok else '未触发 ❓（调大 rounds 再试）'}")
        print("=" * 60)


async def _collect(engine: object, events: list) -> None:
    """旁路订阅全事件用于收尾统计。"""
    async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
        events.append(ev.msg)


if __name__ == "__main__":
    asyncio.run(main())
