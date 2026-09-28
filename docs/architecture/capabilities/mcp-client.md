# Capability: mcp-client（taifeng 作为 MCP 客户端）

## Purpose

taifeng 连外部 MCP server（stdio 子进程 / streamable HTTP），把其工具桥接进 `ToolRegistry`，并与
server 完成双向 JSON-RPC：本端发请求，server 也会在处理途中反向发请求（`elicitation/create`、`ping`）。
本契约覆盖**协议版本协商**、**tools/call 结果投影**、**server → client 请求路由与 elicitation 注入口**。
工具集的注册 / 同步见 [dynamic-tool-set](dynamic-tool-set.md)；taifeng 作为 server 的一侧见
[mcp-server](mcp-server.md)。

修复的缺口（2026-09-28 review）：

- 结果投影把 image 降级成 `[image: mime]`、丢弃 `structuredContent`、把其余内容块（含 base64）`json.dumps`
  进文本——模型看不见 MCP 工具取回的图，读不到只以结构化结果返回的数据；
- server 发来的带 id 请求被记 debug 后丢弃，server 只能干等超时；stdio 读循环先按 id 匹配，server 请求
  与本端请求撞号时被误当成响应；
- stdio 声明 `2024-11-05`、HTTP 声明 `2025-03-26`、server 恒回 `2024-11-05`，客户端从不校验 server 回的版本。

参照：MCP 规范 2025-06-18（lifecycle / transports / tools / elicitation / cancellation）；codex
`codex-rs/rmcp-client`、`codex-rs/protocol/src/models.rs`。决策：ADR 0063。
实现：`mcp/protocol.py`、`mcp/content.py`、`mcp/elicitation.py`、`mcp/server_messages.py`，接线
`mcp/stdio_client.py`、`mcp/http_client.py`、`mcp/bridge.py`、`mcp/server.py`。

## 数据契约

### 协议版本（`mcp/protocol.py`）

| 符号 | 含义 |
| --- | --- |
| `LATEST_PROTOCOL_VERSION = "2025-06-18"` | 客户端 initialize 声明的版本；server 协商的首选 |
| `SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")` | 客户端可接受 / server 可原样回的版本（新 → 旧） |
| `negotiate_protocol_version(result) -> str` | 客户端校验 initialize 响应；清单外 / 缺失 / 非字符串 → `McpProtocolVersionError` |
| `select_server_protocol_version(requested) -> str` | server 侧：请求版本受支持原样回，否则回最新版 |
| `McpProtocolVersionError(McpToolError)` | code `-32602`；`requested` / `received` / `supported` 属性 |
| `McpStdioClient.protocol_version` / `McpHttpClient.protocol_version` | 协商结果；握手前为 `None` |

### tools/call 结果投影（`mcp/content.py`）

`convert_tool_result(result, *, attach_images) -> McpToolOutput{text, is_error, attachments, structured_content}`，
形状非法抛 `McpContentError`。桥 handler 据此构造 `ToolResult`：

| MCP 内容 | `ToolResult` |
| --- | --- |
| `text` | 原文进 `output`（各块按出现顺序换行拼接） |
| `image` | `attach_images=True`（默认）→ `ImageAttachmentV1.from_bytes` 进 `attachments`，按出现顺序排在全部文本之后；`False` → `[image: <mime>, <n> bytes, not attached (attach_images=False)]` |
| `audio` | `[audio: <mime>, <n> bytes, not shown: audio content is unsupported]` |
| `resource`（text） | `[resource <uri> (<mime>)]` + 换行 + 原文 |
| `resource`（blob） | `[resource <uri>: <mime>, <n> bytes binary, not shown]` |
| `resource_link` | `[resource link <name> <<uri>> (<mime>): <description>]` |
| 其他 `type` | `[unsupported MCP content type '<type>']` |
| `structuredContent` | 原对象进 `data["structured_content"]`（`data["mcp_tool"]` 保留）；`output` 里没有与之 JSON 等价的 text 块时补一段 `json.dumps(..., ensure_ascii=False)` |
| `isError` | `is_error` |

`extract_text_content(result) -> (text, is_error)` 保留为公共符号：同一套文本规则、图片恒为占位、不产附件。

### elicitation 注入口（`mcp/elicitation.py`）

| 符号 | 含义 |
| --- | --- |
| `ElicitationHandler`（Protocol） | `async (ElicitationRequest) -> ElicitationResult`；可直接传 `async def` 函数 |
| `ElicitationRequest{message, requested_schema, server_info}` | server 给的说明、扁平 object schema、发起方 serverInfo |
| `ElicitationResult{action, content}` | `action ∈ {accept, decline, cancel}`；`content` 仅 accept 可带、值限 str/int/float/bool；违例构造期 `ValueError` |
| `McpStdioClient(..., elicitation_handler=)` / `.spawn(..., elicitation_handler=)` | 可选注入 |
| `McpHttpClient(..., elicitation_handler=)` / `.connect(..., elicitation_handler=)` | 可选注入 |

### server → client 路由（`mcp/server_messages.py`）

`ServerMessageRouter` 两种传输共用：`route(msg)` 在读循环内同步调用，所有应答 / 监听以 task 调度；
`aclose()` 取消并等待全部任务。

| 消息 | 处理 |
| --- | --- |
| `ping` 请求 | 回 `{}` |
| `elicitation/create`，已注入 handler | 交 handler，按下文「应答规则」回复 |
| `elicitation/create`，未注入 | `-32601 Method not found` |
| 其他请求 | `-32601` |
| id 非字符串 / 整数（含 bool） | `-32600`，`id: null` |
| 与在飞请求重号 | `-32600` |
| `notifications/cancelled` | 取消对应在飞应答任务，不回响应；未知 / 已完成 / 形状不对的 id 忽略 |
| `notifications/tools/list_changed` | 以 task 调度监听者（`bind_mcp_tools` 登记） |

## 行为契约

### Requirement: 版本协商不静默继续

客户端 initialize SHALL 声明 `LATEST_PROTOCOL_VERSION`，并 SHALL 校验 server 回的 `protocolVersion`：
在 `SUPPORTED_PROTOCOL_VERSIONS` 内则记为协商结果；否则（含缺失）SHALL 关闭连接并抛
`McpProtocolVersionError`——stdio 关子进程，HTTP 以 DELETE 结束已分配的会话，且不发 `initialized`。

HTTP 下，协商后的每个请求（POST、GET 推送流、关闭时的 DELETE）SHALL 带
`MCP-Protocol-Version: <协商版本>`；initialize 本身不带。

server 侧：客户端请求的版本受支持 SHALL 原样回，否则回最新版（不以 JSON-RPC 错误拒绝版本）。

#### Scenario: 旧 server
- **WHEN** server 回 `protocolVersion: "2024-11-05"`
- **THEN** 建连成功，`protocol_version == "2024-11-05"`；elicitation 等 2025-06-18 增量能力只是不会出现

#### Scenario: 未知版本
- **WHEN** server 回 `"2099-01-01"`
- **THEN** `spawn` / `connect` 抛 `McpProtocolVersionError`，连接已关闭

### Requirement: 结果投影不丢内容

非文本内容 SHALL 按上表投影；base64 正文 SHALL NOT 进 `output`。形状非法（`content` 非数组、块非对象、
缺必填字段、图片 MIME 不在 PNG/JPEG/WebP/GIF、base64 非法或为空、`structuredContent` 非对象）SHALL 使
该次调用判错：`ToolResult.error("mcp_invalid_content: …", reason="mcp_invalid_content", mcp_tool=<名>)`。

图片附件 SHALL 走 [tool-image-attachment](tool-image-attachment.md) 的落盘前 admission：桥只做「能否表达为
`ImageAttachmentV1`」的形状校验；数量 / 字节 / MIME 白名单 / 尺寸 / 帧数由 loop 按宿主注入的
`ImageInputPolicy` 执行。宿主未启用策略时，带图的 MCP 调用按该契约以 `tool_attachment_rejected` 判错；
不需要看图的宿主以 `attach_images=False` 显式选占位档。

#### Scenario: 截图工具
- **WHEN** 已绑定的 MCP 工具返回 `[text "页面截图", image/png]`，宿主启用了 `ImageInputPolicy`
- **THEN** fco 的 `output == "页面截图"`，`attachments` 含 1 张 PNG，模型下一轮看得见图

#### Scenario: 只返回结构化结果
- **WHEN** 结果为 `{"content": [], "structuredContent": {"t": 22.5}}`
- **THEN** `output == '{"t": 22.5}'`，`data == {"mcp_tool": ..., "structured_content": {"t": 22.5}}`

### Requirement: elicitation 按注入与否声明并应答

注入 handler SHALL 在 initialize 的 `capabilities` 声明 `elicitation: {}`；未注入 SHALL NOT 声明。

应答规则：params 非对象 / `message` 非字符串 / `requestedSchema` 不是 `type: object` 的对象 → `-32602`；
handler 抛异常 → `-32603`，消息只含异常类型名（细节进本地日志，不回传可能不可信的 server）；返回值不是
`ElicitationResult` → `-32603`；accept 的 `content` 违反 `requestedSchema`（`tool-argument-validation` 的
schema 子集）→ `-32603`；否则回 `{"action": ..., "content"?: ...}`。任何失败 SHALL NOT 影响连接存活。

#### Scenario: 撞号
- **WHEN** server 发的 `elicitation/create` 恰好复用了本端某个在飞 `tools/call` 的 id
- **THEN** 该消息按请求路由（先看 `method` 再按 id 结算），本端 `tools/call` 不被错误结算

### Requirement: 取消与关闭

handler 调用 SHALL 可被两种方式打断：server 的 `notifications/cancelled`（取消后不回响应）与客户端
`close()`（先 `aclose` 路由器，再关传输）。内核 SHALL NOT 另设 handler 超时：规范把请求超时与取消通知
归于请求发送方（server）；宿主要自有时限可在 handler 内限时后返回 `cancel`。

`tools/call` 途中发生的 elicitation，其等待用户的时间计入该次调用的超时（stdio
`request_timeout_seconds`、HTTP `request_timeout_seconds`、桥 `timeout_seconds`）；需要人工交互的宿主应相应调大。

## 测试接入

- `tests/mcp/test_protocol.py` —— 版本常量、客户端协商（接受 / 拒绝 / 缺失）、server 协商、stdio 断开子进程、HTTP 版本头与会话结束
- `tests/mcp/test_content.py` —— 各内容类型投影、非法形状、structuredContent 去重 / 补全、桥 handler、经 loop admission 落 fco（策略启用 / 未启用）
- `tests/mcp/test_elicitation.py` —— 两种传输：能力声明、accept / decline、未注入 -32601、handler 抛错、撞号、ping、未知方法、server 取消、close 打断
- `tests/mcp/test_server_messages.py` —— 路由器单测：非法 / 重号 id、未知取消、监听派发、`aclose`、投递失败

## 能力边界（如实记录）

- **不校验 `outputSchema`**：规范 SHOULD 由客户端按工具的 `outputSchema` 校验 structuredContent；当前未做（同名
  工具 outputSchema 变化还需纳入 `McpToolBinding.sync` 的替换判定）。
- **不支持** `sampling/createMessage`、`roots/list`、`resources/*`（作为客户端）、`prompts/*`、进度通知、分页
  `nextCursor`、HTTP 流续传（`Last-Event-ID`）；对应的 server 请求一律 `-32601`。
- **本端超时 / 取消时不发 `notifications/cancelled`**：`tools/call` 在客户端超时后，server 侧可能仍在等
  elicitation 应答，直到它自己的超时。
- `resource_link` 只给引用，不代为 `resources/read`；blob 资源（即使是图片）只给占位。
- audio 无内核模态，恒为占位。

## R1–R5 影响

R1：纯协议机制，UI 与用户交互全在宿主 handler；R2：附件随 fco 追加在 tail，与既有工具图片同路；R3：
客户端不在 engine 事件总线上，路由器以日志记录取消 / 投递失败 / handler 异常，工具侧结果仍经
`tool_call_completed`；R4：handler 与监听均为可取消 task，`close()` 统一收敛；R5：无持久化状态，
重启后由宿主重新建连与绑定。
