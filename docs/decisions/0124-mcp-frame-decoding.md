# ADR 0124：MCP 帧解析共享

状态：Accepted

## 决策

stdio 与 SSE 使用同一 UTF-8 JSON 对象解析器。坏帧记录类型和长度后跳过；HTTP 单响应坏形状明确拒绝。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
