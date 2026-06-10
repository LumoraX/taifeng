"""声明式 skill 编排 demo —— orchestration 三原语（parallel / serial / when）。

场景：

    trip-planner (composite entry, 声明了 orchestration)
       ├─ ① parallel: [route-north, route-south]   ← 两条线路并发规划（互不依赖）
       ├─ ② serial:   [weather-probe]              ← 探测是否需要天气（产出 needs_weather flag）
       ├─ ③ when needs_weather → [weather-detail]   ← 条件分支：仅判定需要时才查天气
       └─ ④ serial:   [itinerary-summarizer]       ← 汇总前序全部输出为最终行程

演示价值：与「LLM 自主 fan-out」（见 travel_planner demo）对照——这里的并发 / 顺序 /
条件**骨架由 SKILL.md 声明确定性驱动**（entry 不采样 LLM），而非靠 LLM 临场决定。
并行段复用底层 dispatch_batch（与 A 同一套并发机制）。

可视化（attach_console_sink 着色打印）：
    [TURN START]      根 turn（trip-planner 编排器）启动
    [ORCH PLAN]       orchestration_plan_resolved —— 声明被解析为执行计划
    [TOOL BATCH]      tool_batch_dispatched —— 一批（并发/串行）合成 call_skill 派发
    [SKILL DISP/RET]  每个子 skill 的派发 / 返回
    [TURN DONE]       根 turn 完成

运行（SimClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/orchestration/demo.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import taifeng
from taifeng.llm.providers.sim import SimTurn, RoutingSimClient
from taifeng.telemetry import attach_console_sink

# 本 demo 的 skill 包目录（与 web_ui demo 复用同一份 skills）
SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingSimClient:
    """按子 skill 的 body 标记路由回放（编排 entry 不采样 LLM，故无需 entry 路由）。

    weather-probe 返回 `{"needs_weather": true}` → 触发 when 的 then 分支（weather-detail）。
    各子 skill 一轮即返回文本（atomic，无 tool call）。
    """
    return RoutingSimClient(routes={
        "ROUTE_NORTH_MARK": [SimTurn(text="北线：D1 出发→山区古镇，D2 湖区，D3 返程。")],
        "ROUTE_SOUTH_MARK": [SimTurn(text="南线：D1 出发→海滨，D2 老城美食，D3 返程。")],
        # 探测步骤：严格输出布尔 flag（when 据此判定）
        "WEATHER_PROBE_MARK": [SimTurn(text='{"needs_weather": true}')],
        "WEATHER_DETAIL_MARK": [SimTurn(text="沿途多晴，山区昼夜温差大，备外套；湖区午后阵雨。")],
        "ITINERARY_SUM_MARK": [SimTurn(
            text="推荐南线（美食+海滨更契合）；山区段备保暖，湖区避开午后阵雨。"
        )],
    })


async def main() -> None:
    """跑一次 trip-planner 编排，控制台实时打印事件流。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=threads,
            model_client=_routing_client(),
            compressors=[],
            # 设 >1 让并行段（route-north/south）真并发；serial 段仍强制串行
            max_parallel_tool_calls=4,
        )
        engine = await pool.get_or_create(
            session_id="demo-orchestration",
            entry_skill_id="trip-planner",
        )
        sink_task = attach_console_sink(engine, color=True)

        sub_id = await engine.submit(taifeng.UserMessage(
            text="帮我规划一次 3 天周末出游，给两条线路对比并按需提示天气。",
        ))
        # 编排 entry 会派发多个子 turn（各自也 emit turn_completed），故只在
        # 根 turn（is_root=True）完成 / 失败时收尾。
        async for ev in engine.subscribe(sub_id):
            done = ev.msg.kind in ("turn_completed", "turn_failed")
            if done and ev.msg.data.get("is_root"):
                break

        # SimClient 瞬时跑完，console_sink 是异步独立订阅——给它一点时间把
        # 缓冲的事件全部打印出来再收尾（真实 LLM 因网络延迟通常无需等待）。
        await asyncio.sleep(0.5)
        await pool.close()
        await asyncio.sleep(0.2)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
