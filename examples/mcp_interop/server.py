"""官方 MCP Python SDK 写的参照 server（互通验证用，见 ``verify.py``）。

不依赖 taifeng：它代表「别人写的 MCP server」。工具三个：

- ``add``：结构化结果；
- ``echo``：纯文本结果；
- ``install_extra``：运行期再注册一个工具 ``extra`` 并通知 ``tools/list_changed``。

运行（由 ``verify.py`` 拉起，不单独使用）：
    python server.py stdio
    python server.py http <port>
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import Context, MCPServer

server = MCPServer("interop-reference", version="1.0.0")


@server.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """原样返回文本。"""
    return text


def extra() -> str:
    """运行期才出现的工具。"""
    return "extra-ready"


@server.tool()
async def install_extra(ctx: Context) -> str:
    """注册 ``extra`` 工具并通知客户端工具集变了。"""
    server.add_tool(extra)
    # 2025-06-18 及更早版本的客户端靠这条通知得知工具集变化；更新的协议版本改走
    # subscriptions/listen（``ctx.notify_tools_changed``），内核尚未实现，这里不发
    await ctx.session.send_tool_list_changed()
    return "installed"


if __name__ == "__main__":
    if sys.argv[1] == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
