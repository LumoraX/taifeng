# Capability: dynamic-tool-set

## Purpose

工具集可以在运行期变化——业务热插拔工具、MCP server 推 `tools/list_changed`——而内核能正确地
**增 / 删 / 替换**、**通知**（R3）并在下一次采样生效，cache 失效被如实归因。

修复的缺口（2026-09-28 review）：`ToolRegistry` 只有 `register`，没有 `unregister` / 替换 / 变更事件；
MCP 客户端忽略全部服务端通知，只有 stdio 传输；prompt 结构指纹只比较工具名，同名 schema 变化导致的
cache 失效会被误记为 `unknown_drop`。

参照：codex `codex-rs/rmcp-client`（streamable HTTP + list_changed 重拉）；opencode `mcp/index.ts`。
决策：ADR 0048。

## 数据契约

### `ToolRegistry`

| 成员 | 含义 |
| --- | --- |
| `register(spec)` | 新增；同名 → `DuplicateToolError` |
| `unregister(name) -> ToolSpec` | 移除并返回；未注册 → `UnknownToolError`（不静默忽略） |
| `replace(spec)` | 同名替换（描述 / schema / handler）；未注册 → `UnknownToolError` |
| `version: int` | 每次变更单调 +1 |
| `subscribe(listener) -> unsubscribe` | 变更后同步回调 `ToolSetChange`；监听者异常记日志并隔离 |

`ToolSetChange = {added, removed, replaced: tuple[str,...], version: int}`。

### `tool_set_changed` 事件

EnginePool 订阅共享注册表，变更时向每个活跃 engine emit（`submission_id="*"`），
`data = ToolSetChange.as_dict()`。pool 关闭时退订。

### MCP 客户端

协议版本协商、tools/call 结果投影、server → client 请求与 elicitation 注入口见
[mcp-client](mcp-client.md)；本节只列与工具集同步相关的符号。

| 符号 | 含义 |
| --- | --- |
| `McpClient`（Protocol） | `list_tools`（跟完 `nextCursor` 分页后的完整列表）/ `call_tool` / `server_info` / `add_tools_changed_listener` |
| `McpStdioClient` | stdio 传输；server 主动消息交 `ServerMessageRouter`，`notifications/tools/list_changed` 以 task 调度监听者 |
| `McpHttpClient.connect(url, headers=, request_timeout_seconds=, listen_notifications=, elicitation_handler=, max_list_pages=, max_stream_resumptions=, stream_resume_delay_seconds=)` | streamable HTTP：POST JSON-RPC，响应 JSON 或 SSE（断流按 `Last-Event-ID` 续传）；`Mcp-Session-Id` 与协商后的 `MCP-Protocol-Version` 带回；GET 推送流（405 = 不支持，不监听；被关闭后带 `Last-Event-ID` 重连）；close 时 DELETE 会话 |
| `bind_mcp_tools(client, registry, tool_prefix=, parallel_safe=, timeout_seconds=, watch=True, trust_annotations=False, attach_images=False) -> McpToolBinding` | 注册并随 list_changed 同步；`trust_annotations` 见下文「副作用分类」；`attach_images` 见 [mcp-client § 结果投影](mcp-client.md) |
| `McpToolBinding.sync() -> (added, removed, replaced)` / `.detach()` | 手动同步 / 卸载本绑定拥有的全部工具；同名工具在描述 / inputSchema / 副作用分类 / outputSchema 任一变化时替换（outputSchema 校验见 [mcp-client](mcp-client.md)） |
| `register_mcp_tools_async(...)` | 一次性注册（旧接口；= `bind_mcp_tools(watch=False)`） |

传输失败、HTTP 非 2xx、超时、续传失败统一为 `McpToolError(-32000, ...)`；JSON-RPC error 保留原 code；
协议版本不受支持为其子类 `McpProtocolVersionError(-32602)`；`tools/list` 翻页失控为其子类 `McpPaginationError(-32000)`。

## 行为契约

### Requirement: 下一次采样生效

工具列表每次采样前按「声明层可见集 ∩ 注册表」重算：变更 SHALL 在下一次采样生效（含同一 turn 的
后续迭代）。已发出但尚未派发、指向被删工具的调用由派发层以 `not_in_registry` 错误结果核销。

### Requirement: cache 失效如实归因

prompt 结构指纹的 tools 分量 SHALL 覆盖每个可见工具的 **名称 + 描述 + input_schema**；同名替换
导致的 cache 失效归因为 `tool_spec_changed`（预期内），而非 `unknown_drop`。

### Requirement: 绑定只管自己的工具

`McpToolBinding` 只增删 / 替换自己拥有（`owned`）的本地名；与他人已注册工具重名 → 跳过并告警，
不抢占。同步串行化（并发通知排队）；同步失败只记日志，不打断宿主。

#### Scenario: server 工具集变化
- **WHEN** 已绑定的 server 推 `notifications/tools/list_changed`，新列表新增 `beta`、`alpha` 描述变化
- **THEN** 注册表新增 `beta`、替换 `alpha`，各 engine 收到两次 `tool_set_changed`，下一次采样可见 `beta`

### Requirement: MCP 工具副作用分类默认保守

桥接出的 ToolSpec SHALL 显式带 `effect_kind` / `reconciliation`（不得落 ToolSpec 默认的 `pure`）：

| 条件 | `effect_kind` / `reconciliation` | `parallel_safe` |
| --- | --- | --- |
| `trust_annotations=False`（默认） | `external_non_idempotent` / `manual` | 绑定参数 |
| 信任且 `annotations.readOnlyHint is true` | `pure` / `none` | `True` |
| 信任且 `annotations.idempotentHint is true` | `idempotent` / `retry` | 绑定参数 |
| 信任但未声明 / 值非字面 `true` / annotations 非对象 | `external_non_idempotent` / `manual` | 绑定参数 |

默认不读 annotations：MCP 规范规定它们只是提示，不可信 server 的提示不得据以决策。分类决定崩溃恢复
（[tool-crash-reconciliation](tool-crash-reconciliation.md)：pure / idempotent 告知可重发，其余挂起交人）
与 strict audit 落账。重新同步时分类变化同样触发 `replace`。

#### Scenario: 默认绑定的写工具崩溃
- **WHEN** 以默认参数绑定的 MCP 工具执行途中进程崩溃
- **THEN** 冷恢复 SHALL 挂起为 `TOOL_OUTCOME_UNKNOWN` 交人裁决，而非告知模型可安全重发

## R1–R5 影响

R1：纯机制，鉴权头由宿主注入；R2：变更天然破坏 tools 前缀，归因为预期失效；R3：`tool_set_changed`；
R4：监听者 task 随客户端 close 取消；R5：注册表是进程内运行态，重启后由宿主重新绑定。
