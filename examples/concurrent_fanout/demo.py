"""并发 fan-out demo —— LLM 自主在一条消息里并行派发多个独立子 skill（能力 A）。

场景：

    research-fanout (composite entry，**无 orchestration 声明**)
       └─ turn 1：LLM 在一条 assistant 消息里同时发起 3 个 call_skill（fan-out）
            ├─ source-web        ┐
            ├─ source-academic   │ max_parallel_tool_calls>1 → 真并发执行
            └─ source-news       ┘ （call_skill 在 runtime 跳锁）
       └─ turn 2：拿到 3 路结果后综合成调研结论

与 orchestration demo 的对照：
    - 这里：并发由 **LLM 临场决定**（把独立调用放进同一条消息）→ 引擎并发派发。
    - 那里：并发由 **SKILL.md 声明** 确定性驱动（parallel 段）。
    两者复用同一套底层 dispatch_batch + Semaphore(max_parallel) + RwLock。

可视化（attach_console_sink）：
    [TOOL BATCH]  tool_batch_dispatched{count:3} —— 一条消息里 3 个 call_skill 同批
    [SKILL DISP]  3 个子 skill 并发派发
    [SKILL RET]   3 路结果回流（完成序可能交错）
    [LLM FINAL]   research-fanout 综合结论

运行（MockClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/concurrent_fanout/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.telemetry import attach_console_sink

SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingMockClient:
    """RoutingMockClient：entry 首轮吐 3 个 call_skill（fan-out）、次轮综合；
    三个源各 sleep 0.3s 以体现并发（串行需 ≥0.9s，并发约 0.3s）。

    按各 skill body 内的唯一标记路由，回放与子 turn 完成顺序无关。
    """
    return RoutingMockClient(routes={
        "FANOUT_ENTRY_MARK": [
            MockTurn(text="三个源相互独立，并发检索。", tool_calls=[
                {"id": "f0", "name": "call_skill",
                 "arguments": '{"skill_id": "source-web", "reason": "网络资料"}'},
                {"id": "f1", "name": "call_skill",
                 "arguments": '{"skill_id": "source-academic", "reason": "学术文献"}'},
                {"id": "f2", "name": "call_skill",
                 "arguments": '{"skill_id": "source-news", "reason": "新闻时讯"}'},
            ]),
            MockTurn(text="综合三路：主流观点一致，学术与新闻互为补充，结论可信。"),
        ],
        "SOURCE_WEB_MARK": [MockTurn(text="网络：要点 A/B/C。", delay_seconds=0.3)],
        "SOURCE_ACADEMIC_MARK": [MockTurn(text="学术：文献 X/Y 支持。", delay_seconds=0.3)],
        "SOURCE_NEWS_MARK": [MockTurn(text="新闻：近期进展 Z。", delay_seconds=0.3)],
    })


async def main() -> None:
    """跑一次 research-fanout，控制台打印事件流（观察 3 路并发）。"""
    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=threads,
            model_client=_routing_client(),
            compressors=[],
            # >1 才会并发；设 1 则退化串行（可改这里对比 wall-clock）
            max_parallel_tool_calls=4,
        )
        engine = await pool.get_or_create(
            session_id="demo-fanout",
            entry_skill_id="research-fanout",
        )
        sink_task = attach_console_sink(engine, color=True)

        sub_id = await engine.submit(taifeng.UserMessage(
            text="请就『城市夜间经济的利弊』做一次多源快速调研。",
        ))
        async for ev in engine.subscribe(sub_id):
            done = ev.msg.kind in ("turn_completed", "turn_failed")
            if done and ev.msg.data.get("is_root"):
                break

        # MockClient 瞬时完成，给异步 console_sink 时间把事件打印完整再收尾
        await asyncio.sleep(0.5)
        await pool.close()
        await asyncio.sleep(0.2)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
