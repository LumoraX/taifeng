# ADR 0125：MCP 日志只记录元数据

状态：Accepted

## 决策

不在客户端日志写原始帧、远端 method/ID/reason/content-type；只记录类型、长度、固定错误类别与受支持协议版本。

## 契约

见 [MCP 客户端](../architecture/capabilities/mcp-client.md)。
