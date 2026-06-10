"""MCP client demo —— taifeng 连接外部 MCP server，把其工具当本地工具调用。

场景：

    market-assistant (entry, tool_names=[lookup_stock_price, convert_currency])
       └─ 这两个工具不是内置、也不是 skill，而是来自一个**独立 MCP server 子进程**：
            taifeng 作为 MCP client spawn 它 → 握手 → tools/list → 注册成 ToolSpec
       └─ turn 1：call lookup_stock_price(AAPL)   → 子进程返回价格
       └─ turn 2：call convert_currency(...)       → 子进程返回换算
       └─ turn 3：综合回复

与其余 demo 的对照：能力 A/B/编排都在 taifeng 进程内；这里工具的**实现体在另一个
进程**，taifeng 通过 MCP (JSON-RPC over stdio) 远程调用 —— 这是 infra 四类里唯一
真·跨进程的体现。工具调用 / 结果仍走统一事件流（与内置工具无差别）。

可视化（attach_console_sink）：
    [TOOL]     lookup_stock_price(...) / convert_currency(...)
    [TOOL RET] 子进程经 MCP 返回的文本结果
    [LLM FINAL] market-assistant 综合回复

运行（SimClient 驱动工具调用，**无需 API key**；MCP server 是本地子进程）：

    cd taifeng
    PYTHONPATH=src uv run python examples/mcp_showcase/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from mcp_lib import connect_showcase_mcp

import taifeng
from taifeng.llm.providers.sim import SimTurn, RoutingSimClient
from taifeng.telemetry import attach_console_sink

SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingSimClient:
    """RoutingSimClient：market-assistant 依次调两个 MCP 工具再综合。

    工具名 = 注册进来的 MCP 工具名（无前缀）；runtime 派发到 MCP-backed handler，
    转成 tools/call 打到子进程。
    """
    return RoutingSimClient(routes={
        "MCP_MARKET_MARK": [
            SimTurn(text="先查 AAPL 股价。", tool_calls=[
                {"id": "m0", "name": "lookup_stock_price",
                 "arguments": '{"symbol": "AAPL"}'},
            ]),
            SimTurn(text="再把 192.5 美元换成人民币。", tool_calls=[
                {"id": "m1", "name": "convert_currency",
                 "arguments": '{"amount": 192.5, "from_": "USD", "to": "CNY"}'},
            ]),
            SimTurn(text="AAPL 现价约 $192.5，约合 1390 元人民币（数据来自 MCP server）。"),
        ],
    })


async def main() -> None:
    """连接 MCP server → 跑一次 market-assistant → 控制台看 MCP 工具被调用。"""
    # 1) 作为 MCP client 连上外部 server，拿到注册好的工具 specs
    client, specs = await connect_showcase_mcp()
    print(f"[mcp] connected: {client.server_info.get('name')} "
          f"tools={[s.name for s in specs]}")
    try:
        with tempfile.TemporaryDirectory() as td:
            pool = await taifeng.EnginePool.create(
                skills_dir=SKILLS_DIR,
                threads_dir=Path(td) / "threads",
                model_client=_routing_client(),
                compressors=[],
                # 关键：把 MCP server 的工具作为 extra_tools 注入 pool
                extra_tools=specs,
            )
            engine = await pool.get_or_create(
                session_id="demo-mcp", entry_skill_id="market-assistant",
            )
            sink_task = attach_console_sink(engine, color=True)

            sub_id = await engine.submit(taifeng.UserMessage(
                text="帮我查下 AAPL 股价，并按汇率折算成人民币。",
            ))
            async for ev in engine.subscribe(sub_id):
                done = ev.msg.kind in ("turn_completed", "turn_failed")
                if done and ev.msg.data.get("is_root"):
                    break

            # SimClient 瞬时完成，给异步 console_sink 时间打印完整再收尾
            await asyncio.sleep(0.5)
            await pool.close()
            await asyncio.sleep(0.2)
            sink_task.cancel()
    finally:
        # 关闭 MCP client = 终止 server 子进程（避免僵尸）
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
