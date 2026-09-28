# ADR 0048：工具集运行时增删 + MCP list_changed 同步 + streamable HTTP 传输

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/dynamic-tool-set.md](../architecture/capabilities/dynamic-tool-set.md)；ADR 0036（cache anchor 真相）

## 背景

2026-09-28 内核 review：`ToolRegistry` 只能 `register`；MCP 客户端把所有服务端通知丢进 debug 日志，
`tools/list_changed` 无从接入；只有 stdio 传输（远程 MCP 基本都是 HTTP）；prompt 指纹只比较工具名。

## 决策

1. **注册表是唯一真相 + 同步监听**：`unregister` / `replace` / `version` / `subscribe`。监听回调同步
   执行（注册表本身无 event loop 依赖），EnginePool 在回调里为各 engine 调度异步 emit。未注册时删除 /
   替换显式报错，不静默。
2. **不引入「工具集锁定到 turn」**：工具列表本就每次采样重算，变更在下一次采样生效是最简单且可预期的
   语义；cache 代价由指纹归因如实暴露，而不是冻结工具集到 turn 结束。
3. **指纹含描述与 schema**：只比名字会把同名替换的 cache 失效误判为 unexpected。
4. **桥与传输解耦**：`McpClient` 协议 + `bridge.py`；stdio / HTTP 都实现它。`McpToolBinding` 只管自己
   `owned` 的名字，重名不抢占。
5. **HTTP 用内核已有的 httpx**，不引 MCP SDK；不做 OAuth（R1，头由宿主注入）；GET 推送流 405 时安静
   降级为「仅显式 sync」，有 debug 日志。

## 后果

`register_mcp_tools_async` 语义不变（内部复用绑定、不监听）；返回列表改为按本地名排序。
`register_mcp_tools` / `McpToolError` / `_extract_text_content` 保留旧导入路径。
