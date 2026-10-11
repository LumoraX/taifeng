# ADR 0128：MCP 关闭的异常优先级

状态：Accepted

## 决策

关闭依次尝试全部资源清理，HTTP pool 无条件得到关闭机会，多个异常保留第一个；握手异常优先于清理异常。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
