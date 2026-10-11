# ADR 0123：MCP 握手取消收敛

状态：Accepted

## 决策

握手失败和取消均关闭客户端，释放进程或 HTTP pool；清理异常不覆盖原始握手异常。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
