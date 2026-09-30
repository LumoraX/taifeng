# Capability: mcp-client（taifeng 作为 MCP 客户端）

## Purpose

taifeng 连外部 MCP server（stdio 子进程 / streamable HTTP），把其工具桥接进 `ToolRegistry`，并与
server 完成双向 JSON-RPC：本端发请求，server 也会在处理途中反向发请求（`elicitation/create`、`ping`）。
本契约覆盖**协议版本协商**、**tools/list 分页**、**tools/call 结果投影与 outputSchema 校验**、
**本端放弃请求时的取消通知**、**streamable HTTP 断流续传**、**server → client 请求路由与 elicitation 注入口**。
工具集的注册 / 同步见 [dynamic-tool-set](dynamic-tool-set.md)；taifeng 作为 server 的一侧见
[mcp-server](mcp-server.md)。

入口都在稳定层（`taifeng.McpStdioClient` / `McpHttpClient` / `McpClient` / `bind_mcp_tools` /
`register_mcp_tools_async`，ADR 0110）。**互通验证**：`examples/mcp_interop/verify.py` 拿官方 MCP Python SDK
写的 server 当对端，真实 stdio 子进程与真实 HTTP 连接各跑一遍（版本协商、`tools/list`、文本与结构化
结果、server 拒绝参数、`tools/list_changed` 后绑定同步）；运行需要官方 SDK：
`PYTHONPATH=src uv run --with mcp python examples/mcp_interop/verify.py`。改动 MCP 客户端后重跑。

修复的缺口（2026-09-28 review）：

- 结果投影把 image 降级成 `[image: mime]`、丢弃 `structuredContent`、把其余内容块（含 base64）`json.dumps`
  进文本——模型看不见 MCP 工具取回的图，读不到只以结构化结果返回的数据；
- server 发来的带 id 请求被记 debug 后丢弃，server 只能干等超时；stdio 读循环先按 id 匹配，server 请求
  与本端请求撞号时被误当成响应；
- stdio 声明 `2024-11-05`、HTTP 声明 `2025-03-26`、server 恒回 `2024-11-05`，客户端从不校验 server 回的版本。

修复的缺口（2026-09-29，ADR 0069）：

- `tools/list` 只取第一页：分页 server 的其余工具对模型不可见，`list_changed` 重新同步时还被当成「已删除」；
- 忽略工具声明的 `outputSchema`：server 违约的结构化结果直接进模型上下文；
- 本端超时 / 取消时不发 `notifications/cancelled`：server 侧处理（可能正等 elicitation 应答）要跑到它自己的超时；
- HTTP 的 SSE 流在响应前断开即失败（或干等超时），即便 server 支持续传；GET 推送流被 server 关闭后再也收不到通知。

参照：MCP 规范 2025-06-18（lifecycle / transports / tools / elicitation / utilities: cancellation、pagination）；
codex `codex-rs/rmcp-client`、`codex-rs/protocol/src/models.rs`；modelcontextprotocol typescript-sdk / python-sdk
（outputSchema 校验、取消通知）。决策：ADR 0063、ADR 0069。
实现：`mcp/protocol.py`、`mcp/content.py`、`mcp/elicitation.py`、`mcp/server_messages.py`、`mcp/pagination.py`、
`mcp/output_schema.py`、`mcp/cancellation.py`、`mcp/sse.py`，接线 `mcp/stdio_client.py`、`mcp/http_client.py`、
`mcp/bridge.py`、`mcp/server.py`。

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

### tools/list 分页（`mcp/pagination.py`）

两种传输的 `list_tools()` 都返回**跟完 `nextCursor` 后的完整列表**（`McpClient` 协议语义），`McpToolBinding.sync`
据此做增删 / 替换判定。首页请求不带 `params`，后续页带 `{"cursor": <上一页 nextCursor>}`（原样回传，不解析）。

| 符号 / 情形 | 含义 |
| --- | --- |
| `max_list_pages`（stdio `spawn` / `__init__`、HTTP `connect` / `__init__`，默认 `DEFAULT_MAX_LIST_PAGES = 100`） | 单次 `tools/list` 的页数上限；< 1 构造期 `ValueError`（stdio 在拉起子进程之前） |
| `nextCursor` 缺失 / `null` / 空串 | 结束 |
| `nextCursor` 非字符串、同一游标出现第二次、页数超限 | `McpPaginationError`（`McpToolError` 子类，code `-32000`，`pages` 属性）；**不**返回已取到的部分 |
| 某页 result 非对象 / `tools` 非数组或缺失 | `McpToolError(-32000)`（不当成空列表） |
| `list_all_tools(fetch_page, *, max_pages)` | 与传输无关的翻页循环（`fetch_page(cursor) -> result`） |

### tools/call 结果投影（`mcp/content.py`）

`convert_tool_result(result, *, attach_images) -> McpToolOutput{text, is_error, attachments, structured_content}`，
形状非法抛 `McpContentError`。桥 handler 据此构造 `ToolResult`：

| MCP 内容 | `ToolResult` |
| --- | --- |
| `text` | 原文进 `output`（各块按出现顺序换行拼接） |
| `image` | `attach_images=True` → `ImageAttachmentV1.from_bytes` 进 `attachments`，按出现顺序排在全部文本之后；`False` → `[image: <mime>, <n> bytes, not attached]` |
| `audio` | `[audio: <mime>, <n> bytes, not shown: audio content is unsupported]` |
| `resource`（text） | `[resource <uri> (<mime>)]` + 换行 + 原文 |
| `resource`（blob） | `[resource <uri>: <mime>, <n> bytes binary, not shown]` |
| `resource_link` | `[resource link <name> <<uri>> (<mime>): <description>]` |
| 其他 `type` | `[unsupported MCP content type '<type>']` |
| `structuredContent` | 原对象进 `data["structured_content"]`（`data["mcp_tool"]` 保留）；`output` 里没有与之 JSON 等价的 text 块时补一段 `json.dumps(..., ensure_ascii=False)` |
| `isError` | `is_error` |

`extract_text_content(result) -> (text, is_error)` 保留为公共符号：同一套文本规则、图片恒为占位、不产附件。

### outputSchema 校验（`mcp/output_schema.py`，桥 handler 执行）

| 情形 | `ToolResult` |
| --- | --- |
| 工具未声明 `outputSchema` | 不校验 |
| `isError: true` | 不校验，按上表投影 |
| 声明了 outputSchema，缺 `structuredContent` | `ToolResult.error("mcp_output_schema_violation: $: structuredContent is missing …", reason="mcp_output_schema_violation", mcp_tool=<名>, violations=[…])` |
| `structuredContent` 违反 outputSchema | 同上，`violations` 为确定的违例清单（≤8 条）；server 的原文**不**进 `output` |
| 合规 | 按上表投影 |

- 校验器：`taifeng.tool.arg_validation.schema_violations`（type / required / properties / enum / const / items /
  additionalProperties:false；不认识的关键字放过，只报确定的违例）。
- `parse_output_schema(meta)`：outputSchema 须为 `type: "object"` 的对象，否则 `McpOutputSchemaError`——桥**跳过该工具并告警**。
- `McpToolBinding.output_schemas: dict[本地名, schema]`：已注册且声明了 outputSchema 的工具；`sync` 时 outputSchema
  的增 / 改 / 删与描述、inputSchema、副作用分类一样触发 `replace`（handler 闭包随之更新）。

### 取消通知（`mcp/cancellation.py`）

| 符号 | 含义 |
| --- | --- |
| `cancelled_notification(request_id, reason)` | `{"method": "notifications/cancelled", "params": {"requestId", "reason"}}`；reason 截断到 200 字符 |
| `CancelNotifier(send)` | 两种传输各持一个：`notify(request_id, method, reason)` 以后台任务发出；`initialize` 不发；投递失败只记日志；`aclose()` 在 `close()` 时取消未发出的通知 |
| `cancel_reason(exc)` | `CancelledError` 的消息即原因；无消息 → `DEFAULT_CANCEL_REASON = "request cancelled by client"` |
| `await_or_abandon(call, *, timeout_seconds, cancel)` | 桥 handler 用：调用与超时、`ToolContext.cancel` 竞速；放弃时 `task.cancel(<原因>)` 打断在飞调用。超时 → `TimeoutError`；token 取消 → `CancelledError`（运行时收敛为 cancelled 结果）；自身被外部取消同样打断调用 |

| 放弃方式 | 通知 `reason` |
| --- | --- |
| 客户端层超时（stdio / HTTP `request_timeout_seconds`） | `client timeout after <N>s` |
| 桥超时（`bind_mcp_tools(timeout_seconds=)`） | `client timeout after <N>s` |
| turn 取消（`ToolContext.cancel`） | `client cancelled the request (<CancelReason>[: <detail>])` |
| 宿主 `task.cancel(msg)` | `msg`；无消息为默认文案 |

### HTTP 断流续传（`mcp/sse.py`，`McpHttpClient` 接线）

| 符号 | 含义 |
| --- | --- |
| `SseCursor{last_event_id, retry_ms, events}` / `.resume_id` | 一条逻辑 SSE 流的续传状态，跨重连复用；id 在事件派发时提交（只带 id 的起点事件也提交），空 id = 清空 |
| `iter_sse_data(resp, cursor)` | WHATWG 事件流解析：`data` 多行拼接、`:` 注释、`id`（含 NUL 忽略）、`retry`（全数字才生效）；中途断开的残余事件不派发，正常 EOF 的残余事件照常派发 |
| `max_stream_resumptions`（`McpHttpClient` / `connect`，默认 `DEFAULT_MAX_STREAM_RESUMPTIONS = 3`） | POST 流续传次数上限；GET 推送流连续无事件重连的上限；0 = 不续传 / 不重连；< 0 构造期 `ValueError` |
| `stream_resume_delay_seconds`（默认 `1.0`） | 续传 / 重连基础间隔，按次数翻倍，单次 ≤30s；server 的 SSE `retry:` 优先；< 0 构造期 `ValueError` |

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

### Requirement: tools/list 跟完分页、失控显式报错

`list_tools()` SHALL 按 `nextCursor` 翻页直到缺失 / `null` / 空串，返回各页 `tools` 的按序拼接。页数超过
`max_list_pages`、游标重复、`nextCursor` 非字符串 SHALL 抛 `McpPaginationError`，SHALL NOT 静默返回部分列表；
某页形状非法 SHALL 抛 `McpToolError`。

#### Scenario: 三页工具
- **WHEN** server 分三页返回 `t1` / `t2` / `t3`
- **THEN** `bind_mcp_tools` 注册全部三个；server 依次收到 `params` 为无、`{"cursor": "cursor-1"}`、`{"cursor": "cursor-2"}`

#### Scenario: 无限翻页
- **WHEN** server 永远返回新的 `nextCursor`，`max_list_pages=4`
- **THEN** 第 4 页后抛 `McpPaginationError`，`bind_mcp_tools` 失败，注册表不留半截工具

### Requirement: outputSchema 不合规的结果不交给模型

工具声明了 `outputSchema` 时，非 `isError` 的结果 SHALL 带合规的 `structuredContent`；缺失或违例 SHALL 使该次调用
判错（`reason="mcp_output_schema_violation"`，`data["violations"]` 列违例），server 原文 SHALL NOT 进 `output`。
outputSchema 变化 SHALL 触发 `sync` 替换。

#### Scenario: 类型不符
- **WHEN** outputSchema 要求 `temperature: number`，结果 `structuredContent = {"temperature": "hot", ...}`
- **THEN** `ToolResult.is_error`，`data["violations"] == ["$.temperature: expected number, got string"]`

### Requirement: 放弃请求即通知 server

本端请求（`initialize` 除外）已发出后因客户端超时、桥超时、turn 取消或调用方取消而放弃时，客户端 SHALL 向 server 发
`notifications/cancelled{requestId, reason}`，并 SHALL 忽略之后迟到的响应。stdio 尚未写出的请求 SHALL NOT 发通知；
HTTP 无从确知 server 是否收到请求体，放弃即发（规范要求接收方忽略未知 id）。通知以后台任务发出，SHALL NOT 阻塞取消
的传播（R4）。

#### Scenario: stdio 超时
- **WHEN** `request_timeout_seconds=0.2`，server 不回 id=2 的 `tools/call`
- **THEN** 调用方得 `McpToolError("request timeout: tools/call")`，server 收到 `{"requestId": 2, "reason": "client timeout after 0.2s"}`

#### Scenario: turn 取消
- **WHEN** 桥接工具执行中 `ToolContext.cancel` 以 `shutdown` 取消
- **THEN** handler 以 `CancelledError` 结束（运行时收敛为 cancelled），server 收到 reason `client cancelled the request (shutdown)`

### Requirement: HTTP 断流按 Last-Event-ID 续传，不支持即显式失败

POST 的 SSE 流在给出本请求响应之前结束（断开或提前 EOF）时：server 给过事件 id → 客户端 SHALL 以
`GET` + `Last-Event-ID: <最新事件 id>` 续传，续传流里的 server 请求 / 通知照常路由，拿到响应即返回；续传流再断则用
最新 id 继续，至多 `max_stream_resumptions` 次。server 从未给过事件 id、续传 GET 回 405 / 其他错误 / 非 SSE、次数
用尽 SHALL 抛 `McpToolError(-32000)`，SHALL NOT 静默等到超时。全部续传计入该请求的 `request_timeout_seconds`。

GET 推送流结束或断开后，客户端 SHALL 带 `Last-Event-ID`（有则带）重连；连续 `max_stream_resumptions` 次重连都没收到
任何事件则放弃并告警（此后工具只在显式 `sync` 时刷新）；收到事件即清零。405 / 错误状态 / 非 SSE 响应 SHALL 停止重连。

#### Scenario: 续传拿到响应
- **WHEN** `tools/call` 的 SSE 流推出 `id: e1` 的事件后断开，server 支持续传
- **THEN** 客户端发 `GET`（`Last-Event-ID: e1`），从续传流拿到响应，调用成功

#### Scenario: server 不带事件 id
- **WHEN** SSE 流在响应前断开，且此前的事件都没有 id
- **THEN** 立即抛 `McpToolError`（`sent no event id to resume from`），不发续传 GET

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
`ImageInputPolicy` 执行。`attach_images` 默认 `False`（占位档，调用照常成功）；显式 `True` 但宿主未启用策略时，
带图的 MCP 调用按该契约以 `tool_attachment_rejected` 判错。

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
`close()`（先 `aclose` 路由器与取消通知发送器，再关传输）。内核 SHALL NOT 另设 handler 超时：规范把请求超时与取消通知
归于请求发送方（server）；宿主要自有时限可在 handler 内限时后返回 `cancel`。

`tools/call` 途中发生的 elicitation，其等待用户的时间计入该次调用的超时（stdio
`request_timeout_seconds`、HTTP `request_timeout_seconds`、桥 `timeout_seconds`）；需要人工交互的宿主应相应调大。

### Requirement: stdio server 可经 `CommandExecutor` 启动（ADR 0112）

`McpStdioClient.spawn(command, executor=)` 给出执行器时 SHALL 经它启动 server：
`CommandSpec(command=shlex.join(command), shell=False, cwd=cwd, env=..., stdin=True)`。`env` 不给时 SHALL
使用最小白名单（`default_safe_env`），不把宿主环境送进执行器；不经执行器的路径维持继承宿主环境。执行器
返回的进程不满足 `StreamingCommandProcess`（或 `stdin` / `stdout` 为 None）时 SHALL 终止该进程并抛
`TypeError`。

客户端 SHALL 持续读走 server 的 stderr，只保留 16 KiB 尾部（`client.stderr_tail`）：不读的话日志写得多的
server 会阻塞在写 stderr 上、不再回应请求。

#### Scenario: server 往 stderr 大量写日志
- **WHEN** server 在应答 `initialize` 之前往 stderr 写了 2 MiB
- **THEN** 握手照常完成
- **AND** `stderr_tail` 是输出的最后一段，长度不超过 16 KiB

## 测试接入

- `tests/mcp/test_protocol.py` —— 版本常量、客户端协商（接受 / 拒绝 / 缺失）、server 协商、stdio 断开子进程、HTTP 版本头与会话结束
- `tests/mcp/test_content.py` —— 各内容类型投影、非法形状、structuredContent 去重 / 补全、桥 handler、经 loop admission 落 fco（策略启用 / 未启用）
- `tests/mcp/test_elicitation.py` —— 两种传输：能力声明、accept / decline、未注入 -32601、handler 抛错、撞号、ping、未知方法、server 取消、close 打断
- `tests/mcp/test_server_messages.py` —— 路由器单测：非法 / 重号 id、未知取消、监听派发、`aclose`、投递失败
- `tests/mcp/test_pagination.py` —— 翻页拼接、结束条件、页数上限 / 重复游标 / 非法游标 / 非法页、旋钮校验；stdio 三页绑定与无限翻页、HTTP 两页 + sync 增删
- `tests/mcp/test_output_schema.py` —— schema 形状、违例判定、桥 handler（合规 / 违例 / 缺失 / isError / 未声明 / 非法 schema 跳过）、sync 替换与重校验、HTTP 端到端
- `tests/mcp/test_cancellation.py` —— 通知报文、原因提取、发送器（initialize 不发 / 投递失败 / 关闭）、`await_or_abandon` 四条路径；stdio 超时 / 完成不发 / 迟到响应 / 桥 token 取消 / 桥超时，HTTP 超时 / 调用方取消
- `tests/mcp/test_sse.py` —— SSE 解析（字段、注释、多行、id 提交与清空、NUL、retry、残余事件、中途断开）、退避、续传响应校验
- `tests/mcp/test_stdio_executor.py` —— 经执行器启动、argv 往返、默认最小环境、无流进程被拒并终止、启动失败上抛、stderr 写满不卡死与尾部上限、本机执行器的 stdin 开关
- `tests/mcp/test_http_resumption.py` —— POST 流续传（断开 / 提前 EOF、续传流内 server 请求路由、次数用尽、无事件 id、405、0 次）、旋钮校验；推送流带 Last-Event-ID 重连、无事件放弃、非 SSE 停止

## 能力边界（如实记录）

- **不支持** `sampling/createMessage`、`roots/list`、`resources/*`（作为客户端）、`prompts/*`、OAuth：均属 ADR 0017
  规则④（「别家有」的产品面，内核无机制缺口）；对应的 server 请求一律 `-32601`，鉴权头由宿主经 `headers` 注入。
- **不处理进度通知**（`notifications/progress`）：记 debug 后忽略，也不据此重置超时。
- **outputSchema 只校验内核子集**：`pattern` / `format` / `oneOf` / `$ref` 等关键字不校验（放过，不误拦）；outputSchema
  也不进模型视野（ToolSpec 无对应字段），模型只从描述与结果推断结构。
- **stdio 无续传**：子进程管道断开即连接结束（未决请求以连接关闭失败）；续传只存在于 streamable HTTP。
- **HTTP 会话过期（404）不自动重建会话**：规范要求客户端以新 initialize 重建；当前按传输错误抛出，由宿主重连并重新绑定。
- `resource_link` 只给引用，不代为 `resources/read`；blob 资源（即使是图片）只给占位。
- audio 无内核模态，恒为占位。

## R1–R5 影响

R1：纯协议机制，UI 与用户交互全在宿主 handler；R2：附件随 fco 追加在 tail，与既有工具图片同路；outputSchema 变化
触发的 `replace` 按 dynamic-tool-set 归因为 `tool_spec_changed`；R3：客户端不在 engine 事件总线上，路由器 / 取消通知 /
续传 / 推送流重连以日志记录，工具侧结果（含 `mcp_output_schema_violation`、`mcp_timeout`、cancelled）仍经
`tool_call_completed`；R4：handler、监听、取消通知均为可取消 task，`close()` 统一收敛，桥接工具响应 `ToolContext.cancel`；
R5：无持久化状态（续传游标只在一次请求 / 一条推送流内有效，规范禁止跨会话持久化分页游标），重启后由宿主重新建连与绑定。
