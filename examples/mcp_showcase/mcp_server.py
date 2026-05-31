"""最小 MCP stdio server —— 给 mcp_showcase demo 当「外部 MCP server」用。

刻意做成**零依赖、纯 stdlib** 的独立脚本（不 import taifeng）：它扮演的是一个
第三方 MCP server，被 taifeng 作为 **MCP client** spawn + 连接 + 注册其工具。

协议：JSON-RPC 2.0 over stdio，逐行（一行一个 JSON）。实现 taifeng McpStdioClient
握手所需的最小子集：
    - initialize            → 回 serverInfo + capabilities
    - notifications/initialized（通知，无 id）→ 不回包
    - tools/list            → 回工具元数据
    - tools/call            → 执行并回 {content:[{type:text,text}], isError}

暴露两个**确定性**工具（便于 mock 回放与真实 LLM 都稳定）：
    - lookup_stock_price(symbol)            查股价（固定表 + 兜底哈希）
    - convert_currency(amount, from_, to)   按固定汇率换算

运行（一般不手动跑，由 demo / web_ui 作为子进程拉起）：
    python examples/mcp_showcase/mcp_server.py
"""

from __future__ import annotations

import json
import sys

# 固定行情/汇率表 —— 演示用，确定性输出。
_STOCK_TABLE = {"AAPL": 192.5, "TSLA": 178.2, "NVDA": 945.0, "MSFT": 421.3}
_RATE_TO_USD = {"USD": 1.0, "CNY": 0.1385, "EUR": 1.08, "JPY": 0.0064}

# tools/list 返回的工具元数据（name / description / inputSchema）。
_TOOLS = [
    {
        "name": "lookup_stock_price",
        "description": "查询某股票代码的最新价格（美元）。",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "股票代码，如 AAPL"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "convert_currency",
        "description": "按固定汇率把金额从一种货币换算到另一种。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from_": {"type": "string", "description": "源货币，如 CNY"},
                "to": {"type": "string", "description": "目标货币，如 USD"},
            },
            "required": ["amount", "from_", "to"],
        },
    },
]


def _stock_price(symbol: str) -> float:
    """查表；未命中用哈希兜底给一个稳定的伪价格（避免随机、保证可复现）。"""
    s = (symbol or "").upper()
    if s in _STOCK_TABLE:
        return _STOCK_TABLE[s]
    return round(50 + (sum(ord(c) for c in s) % 500), 2)


def _convert(amount: float, from_: str, to: str) -> float:
    """两步换算：源→USD→目标。未知货币按 1.0（USD）兜底。"""
    f = _RATE_TO_USD.get((from_ or "").upper(), 1.0)
    t = _RATE_TO_USD.get((to or "").upper(), 1.0)
    return round(amount * f / t, 4)


def _call_tool(name: str, args: dict) -> tuple[str, bool]:
    """执行工具，返回 (文本结果, is_error)。"""
    if name == "lookup_stock_price":
        sym = str(args.get("symbol", "")).upper()
        return f"{sym} 最新价 ${_stock_price(sym)}（来源：mcp_showcase server）", False
    if name == "convert_currency":
        amt = float(args.get("amount", 0))
        f, t = str(args.get("from_", "USD")), str(args.get("to", "USD"))
        return f"{amt} {f.upper()} = {_convert(amt, f, t)} {t.upper()}", False
    return f"unknown tool: {name}", True


def _handle(msg: dict) -> dict | None:
    """处理单条 JSON-RPC 消息；返回响应 dict，或 None（通知不回包）。"""
    method = msg.get("method")
    req_id = msg.get("id")

    # 通知（无 id）：notifications/initialized 等 —— 不回包
    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-showcase", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        text, is_error = _call_tool(params.get("name", ""), params.get("arguments") or {})
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        }
    # 未知方法 → JSON-RPC method not found
    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> None:
    """逐行读 stdin → 处理 → 逐行写 stdout（即时 flush，避免缓冲卡住握手）。"""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
