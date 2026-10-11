# ADR 0122：MCP 与本地进程的有界长行

状态：Accepted

## 决策

行上限 16 MiB；读循环失败后连接拒绝后续请求，所有 pending 显式失败，资源清理由 close 继续完成。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
