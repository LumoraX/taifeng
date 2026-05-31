"""mcp_showcase 的 MCP client 接线 —— 被 demo.py 与 web_ui server.py 共用。

taifeng 作为 **MCP client**：spawn 本目录的 mcp_server.py 子进程、完成握手、把它
暴露的工具注册成 taifeng ToolSpec。返回的 client 持有子进程句柄，调用方收尾时必须
``await client.close()`` 以终止子进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import taifeng
from taifeng.mcp import register_mcp_tools_async
from taifeng.tool import ToolRegistry

# 外部 MCP server 脚本路径（与本文件同目录）
MCP_SERVER = Path(__file__).parent / "mcp_server.py"


async def connect_showcase_mcp() -> tuple[taifeng.McpStdioClient, list[taifeng.ToolSpec]]:
    """spawn mcp_server.py + 注册其工具。

    返回 ``(client, specs)``：specs 传给 ``EnginePool.create(extra_tools=...)``；
    client 由调用方在收尾时 ``await client.close()`` 关闭子进程（避免僵尸进程）。
    用当前解释器（sys.executable）跑子进程，保证与主程序同环境、无需额外依赖。
    """
    client = await taifeng.McpStdioClient.spawn([sys.executable, str(MCP_SERVER)])
    # 一次性 ToolRegistry 只为满足 register 入参；真正生效的是返回的 specs
    # （传给 EnginePool 后由 pool 内部 registry 接管派发）。
    specs = await register_mcp_tools_async(client, ToolRegistry())
    return client, specs
