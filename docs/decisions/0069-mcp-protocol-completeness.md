# ADR 0069：MCP 协议补全——分页、outputSchema、取消通知、HTTP 断流续传与 server 侧 elicitation 能力门控

- 状态：Accepted
- 日期：2026-09-29
- 关联：[capabilities/mcp-client.md](../architecture/capabilities/mcp-client.md)；
  [mcp-server.md](../architecture/capabilities/mcp-server.md)；[dynamic-tool-set.md](../architecture/capabilities/dynamic-tool-set.md)；
  [permission-gate.md § McpPrompter](../architecture/capabilities/permission-gate.md)；ADR 0017 / 0048 / 0063

## 背景

ADR 0063 之后，MCP 契约的「能力边界」仍列着五处与规范 2025-06-18 脱节的地方：

1. **`tools/list` 只取第一页**。分页的 server 其余工具对模型永远不可见；`list_changed` 重新同步时，第一页之外的
   工具还会被判成「已删除」。
2. **不看 `outputSchema`**。规范要求声明了 outputSchema 的工具 MUST 返回合规的 `structuredContent`、客户端 SHOULD
   校验，安全条款另要求「把工具结果交给 LLM 之前先校验」；server 违约的数据此前直接进了模型上下文。
3. **本端放弃请求时一声不吭**。lifecycle「Timeouts」要求请求超时的发送方 SHOULD 发 `notifications/cancelled`；
   客户端超时 / turn 取消后，server 侧（可能正等 elicitation 应答）要一直跑到它自己的超时。桥接工具甚至不响应
   `ToolContext.cancel`，只认超时。
4. **HTTP 断流即失败**。POST 的 SSE 流在给出响应前断开时，要么报「流结束无响应」，要么干等到超时——即便 server
   支持按 `Last-Event-ID` 续传；GET 推送流被 server 关闭（规范允许随时关）后再也收不到 `list_changed`。
5. **server 侧向未声明能力的客户端发 elicitation**。`McpPrompter` 对任何客户端都发 `elicitation/create`，违反
   lifecycle「只使用协商成功的能力」与 elicitation 章节的能力声明 MUST；不支持的客户端若不理，审批要拖到超时才 deny。

## 决策

1. **分页在客户端内跟完**（`mcp/pagination.py`）。`McpClient.list_tools` 的契约就是「完整列表」，桥与 `sync` 无需
   感知分页。首页不带 `params`，后续页原样回传 `nextCursor`；缺失 / `null` / 空串为结束。页数上限
   `max_list_pages`（默认 100）、重复游标、非字符串游标一律抛 `McpPaginationError`（`McpToolError` 子类），**不返回
   部分列表**；页形状非法（result 非对象、`tools` 非数组）同样显式报错，不再当成空列表。
2. **outputSchema 按两家官方 SDK 的一致口径校验**（`mcp/output_schema.py`，桥 handler 执行）：`isError` 结果不校验；
   声明了 outputSchema 却缺 `structuredContent`、或违反 schema → 该次调用判错（`mcp_output_schema_violation`，
   违例进 `data["violations"]`），server 原文不进 `output`。校验器复用 `arg_validation.schema_violations`（不引入
   jsonschema；不认识的关键字放过）。outputSchema 变化纳入 `sync` 替换判定（`McpToolBinding.output_schemas`）；形状非法
   （非 `type: object` 对象）的工具跳过并告警。
3. **放弃即通知**（`mcp/cancellation.py`）。两种传输各持一个 `CancelNotifier`：请求因客户端超时或被取消而放弃时，以
   **后台任务**发 `notifications/cancelled{requestId, reason}`（放弃时本端常处于取消中，再 `await` 会拖慢取消），
   `close()` 统一收敛；`initialize` 不发；stdio 未写出的请求不发，HTTP 放弃即发。桥改用 `await_or_abandon`：调用与
   超时、`ToolContext.cancel` 竞速，放弃时 `task.cancel(<原因>)`，客户端从 `CancelledError` 的消息取原因——
   **原因经取消消息传递**，`McpClient.call_tool` 签名不变，宿主直接调客户端并自行取消时同样发通知。
4. **HTTP 断流续传**（`mcp/sse.py` + `McpHttpClient`）。SSE 解析补齐 WHATWG 语义（id 在派发时提交、只带 id 的起点事件
   也提交、`retry`、注释、NUL）。POST 流在本请求响应前结束（断开或提前 EOF）且 server 给过事件 id → `GET` +
   `Last-Event-ID` 续传，续传流再断用最新 id 继续，至多 `max_stream_resumptions` 次（默认 3，间隔按 server `retry:` 或
   `stream_resume_delay_seconds` 指数退避，单次 ≤30s，全部计入请求超时）。server 从未给 id、续传 GET 回 405 / 错误 /
   非 SSE、次数用尽 → `McpToolError` 显式失败。GET 推送流按 SSE 惯例带 `Last-Event-ID` 重连，**连续**无事件的重连达到
   同一上限即放弃并告警，收到事件即清零。
5. **server 侧按客户端能力门控**（`mcp/server_capabilities.py`）。`McpStdioServer` 在 initialize 记下客户端能力
   （`client_capabilities`）；`server_initiated_request` 写出前按方法查所需能力（`elicitation/create` / `sampling/createMessage`
   / `roots/list`），缺失即**不发请求**、emit `elicitation_unsupported`、抛 `McpClientCapabilityError`；`McpPrompter` 据此立即
   fail-closed 判 deny（`reason="elicitation_unsupported: …"`）。门控放在请求原语而不是 prompter：规范约束的是 server 发出
   的一切请求，任何调用方都应受同一约束。
6. **仍不做**：sampling、roots、resources（作为客户端）、prompts、OAuth——维持 ADR 0017 规则④的裁决，只在契约「能力边界」
   里保留说明。

## 否决的方案

- **分页超限时返回已取到的部分并告警**：截断的工具集会让 `sync` 把后面的工具当成「已删除」注销，比失败更糟，而且是静默的。
- **只靠页数上限、不查重复游标**：重复游标意味着必然打转，提前失败能给出准确原因，不必白翻到上限。
- **outputSchema 违例时照常返回文本、只附违例说明**：规范安全条款要求交给 LLM 之前校验；server 已违反自己声明的契约，
  其内容不可信。两家官方 SDK 在这里都直接失败。
- **outputSchema 形状非法时照常注册、不校验**：等于静默放行；注册后每次调用必败也没有意义——不暴露给模型最诚实。
- **引入 jsonschema 做完整校验**：内核核心依赖保持最小（与 `arg_validation` 同一取舍）；子集已覆盖类型 / 必填 / 枚举等
  确定违例，漏拦的部分由 server 自己对契约负责。
- **给 `McpClient.call_tool` 加 `cancel` / `reason` 参数**：改协议面会波及所有第三方 `McpClient` 实现与测试替身；取消消息
  是 asyncio 原生的承载位，宿主直接 `task.cancel(msg)` 也天然生效。
- **在 `except CancelledError` 里直接 `await` 发通知**：HTTP 下一次 POST 可能再挂一个超时，取消传播被拖住（R4）。
- **HTTP 断流后重发原 POST**：不是幂等的——server 可能已执行，重发会让工具跑两次；规范给的恢复路径就是 `Last-Event-ID` 续传。
- **没有事件 id 时也尝试不带 `Last-Event-ID` 的 GET 续传**：规范禁止 server 在无关的 GET 流上回响应，这样的 GET 拿不回
  本请求的结果，只会拖到超时。
- **推送流只在传输错误时重连、server 正常关流就停**：规范允许 server 随时关推送流，停下来就再也收不到 `list_changed`；
  SSE 标准的 EventSource 也是关流即重连。
- **推送流重连按总次数封顶**：长会话里 server 周期性关流是常态，总次数封顶会让健康的连接在若干小时后失去通知；按「连续
  无事件」计数才区分得出「server 正常推送」与「server 失控」。
- **把 elicitation 门控放在 `McpPrompter`**：只护住一个调用方；`server_initiated_request` 是公开原语，规范约束的是 server
  发出的所有请求。
- **门控同时要求协商版本 ≥ 2025-06-18**：`McpPrompter` 仍兼容定稿前草案的 `reject` 动作，早期客户端可能在旧版本号下声明
  了 elicitation；能力声明才是规范的 MUST，版本不再加码。
- **客户端未声明时仍发、靠 `-32601` 判拒**：违反 MUST；不回应的客户端会让每次审批都拖到超时。

## 验证

`tests/mcp/test_pagination.py`（翻页拼接、结束条件、页数上限 / 重复游标 / 非法游标 / 非法页、旋钮校验；stdio 三页绑定与
无限翻页、HTTP 两页 + sync 增删）、`tests/mcp/test_output_schema.py`（形状、违例判定、桥 handler 合规 / 违例 / 缺失 /
isError / 未声明 / 非法 schema 跳过、sync 替换与重校验、HTTP 端到端）、`tests/mcp/test_cancellation.py`（通知报文、原因
提取、发送器、`await_or_abandon` 四条路径；stdio 超时 / 完成不发 / 迟到响应 / 桥 token 取消 / 桥超时，HTTP 超时 / 调用方
取消）、`tests/mcp/test_sse.py` + `tests/mcp/test_http_resumption.py`（SSE 解析；POST 流续传成功 / 提前 EOF / 续传流内
server 请求 / 次数用尽 / 无事件 id / 405 / 0 次；推送流带 `Last-Event-ID` 重连、无事件放弃、非 SSE 停止）、
`tests/mcp/test_server_capabilities.py`（门控纯函数、未 initialize / 未声明不发且 emit、声明后照发、prompter 立即 deny、
真实 `tools/call` 路径）。依赖旧行为的 `test_server_initiated_request.py` / `test_server_initiated_telemetry.py` /
`test_hitl_e2e.py` 改为先完成 initialize 握手；`examples/mcp_hitl` 改为以 2025-06-18 声明 elicitation 能力。

真实 LLM：`mcp serve` CLI 只走 LiteLLM chat completions，当前代理对该模型不开放 `/v1/chat/completions`（与本变更无关），
改以等价的 codex provider 驱动同一 `McpStdioServer` + `McpPrompter` 链路：声明 elicitation 的客户端收到
`elicitation/create` 并批准后 `call_skill` 正常派发；未声明的客户端不收到请求，审批立即判 deny，turn 照常完成。
