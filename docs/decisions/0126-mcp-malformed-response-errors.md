# ADR 0126：MCP 响应形状的错误归一

状态：Accepted

## 决策

非法 JSON-RPC error/result 与 tools/call 形状统一为 McpToolError；不返回假空成功结果，不泄漏解码异常。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
