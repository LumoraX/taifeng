# mcp-server Specification

## Purpose
TBD - created by archiving change mcp-server-mode. Update Purpose after archive.
## Requirements
### Requirement: McpStdioServer 通过 stdio 暴露 EnginePool

系统 SHALL 提供 `taifeng.mcp.McpStdioServer` 类，构造签名：

```python
McpStdioServer(
    pool: EnginePool,
    *,
    server_name: str = "taifeng",
    server_version: str = taifeng.__version__,
)
```

`McpStdioServer` SHALL **不**自己构造 `EnginePool` —— 业务侧先按既有 `EnginePool.create(...)` 路径配齐 model_client / compressors / hooks 等，再注入 server。Server SHALL 不引入任何业务概念（R1）。

`McpStdioServer.run()` SHALL 实现 JSON-RPC 2.0 协议，循环读 stdin 行 → JSON 解析 → 派发 → 写 stdout 行。每条消息以单行 JSON 编码，行分隔（与 `McpStdioClient` 对称）。

#### Scenario: 实例化不触发 pool 行为
- **WHEN** 业务侧 `McpStdioServer(pool)` 构造
- **THEN** SHALL NOT 触发任何 store / engine 操作
- **AND** SHALL 仅持有 pool 引用 + server 元信息

### Requirement: initialize handshake 合规

`McpStdioServer` SHALL 实现 MCP `initialize` 方法。收到 `{"method": "initialize", ...}` 时返回：

```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": {"tools": {}, "resources": {}},
  "serverInfo": {"name": <server_name>, "version": <server_version>}
}
```

`protocolVersion` SHALL 等于 `"2024-11-05"`（MCP 当前稳定协议版本，与 `stdio_client.py::_initialize` 对齐）。

#### Scenario: initialize 返回标准 handshake
- **WHEN** MCP 客户端发送 `{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {...}}`
- **THEN** server SHALL 返回 result 含 `protocolVersion="2024-11-05"`、`serverInfo.name=server_name`、`capabilities.tools` / `capabilities.resources` 字段

### Requirement: tools/list 暴露单个 meta-tool `run_skill_turn`

`tools/list` 方法 SHALL 返回单个 tool spec：

- `name`: `"run_skill_turn"`
- `description`: 一句话描述（"Run a taifeng skill for one turn and return final assistant text" 或等价中文）
- `inputSchema`: object schema 含必填 `skill_id: string`、`message: string`，可选 `session_id: string`（默认 `"mcp-default"`）

不暴露所有 skill 作为独立 tool —— call_skill 派发受 dispatch_policy 约束需要 entry skill 上下文，过早暴露会破坏 R1。

#### Scenario: tools/list 返回 run_skill_turn
- **WHEN** 客户端调 `tools/list`
- **THEN** result.tools SHALL 含 1 项，name=`"run_skill_turn"`
- **AND** inputSchema.required SHALL 包含 `["skill_id", "message"]`

### Requirement: tools/call 派发到 EnginePool 完整 turn

`tools/call` with `name="run_skill_turn"` SHALL：

1. 校验 `arguments.skill_id` 与 `arguments.message` 必填字符串；缺失 → JSON-RPC error code `-32602 Invalid params`
2. 调 `pool.get_or_create(session_id=<args>, entry_skill_id=<args>)`；未知 skill / 非 entry → 返回 MCP 标准 `{"content":[{"type":"text","text":<error>}], "isError": true}`（不是 JSON-RPC 错误）
3. `engine.submit(UserMessage(text=message))` 拿 sub_id
4. 异步消费 `engine.subscribe(sub_id)`，累积 `assistant_text` deltas；遇 `turn_completed` 或 `turn_failed` 退出
5. 返回 `{"content": [{"type": "text", "text": final_text}], "isError": <bool>}`，`isError=True` 当且仅当 `turn_failed`

**并发模型**：`tools/call` SHALL 以 server 持有的 owned task 派发，读循环在 turn 期间**持续读取 stdin**——turn 内 `McpPrompter` 发起的 `elicitation/create` 才能收到 client 应答（否则必超时 → deny）。task 完成后经 `_write_message` 写回响应；task 内未捕获异常 SHALL 以 `-32603 Internal error` 响应写回，MUST NOT 使读循环退出。`run()` 退出（EOF / 取消）时 SHALL 取消并等待所有在飞 task。其他 method（`initialize` / `tools/list` / `resources/*`）仍在读循环内同步处理，响应顺序与请求顺序一致。

#### Scenario: HITL 在真实 tools/call 路径上获批
- **WHEN** client 发 `tools/call run_skill_turn`，turn 内工具触发 `McpPrompter.prompt`，server 写出 `elicitation/create`，client 随后在 stdin 写入对应 `{"id": "srv_1", "result": {"action": "accept", ...}}`
- **THEN** prompter 在超时前拿到 accept，`tools/call` 最终响应 `isError=False`

#### Scenario: 读循环退出收敛在飞任务
- **WHEN** 一个 `tools/call` 仍在执行时 stdin 到达 EOF
- **THEN** `run()` 返回前该 task 被取消并 await 完成

#### Scenario: 成功 turn 返回 final text
- **WHEN** mock LLM 在一个 turn 内返回 `"foo"` 然后 `completed`
- **AND** 客户端 `tools/call(name="run_skill_turn", arguments={"skill_id": "<entry>", "message": "hi"})`
- **THEN** result.content[0].text SHALL 等于 `"foo"`
- **AND** result.isError SHALL 为 false

#### Scenario: 未知 skill 不抛 JSON-RPC error，走 isError
- **WHEN** 客户端 `tools/call(name="run_skill_turn", arguments={"skill_id": "ghost", "message": "hi"})`
- **THEN** result.isError SHALL 为 true
- **AND** result.content[0].text SHALL 含 `"ghost"`
- **AND** JSON-RPC 顶层 SHALL NOT 是 error 包装（让客户端 LLM 看到原因）

#### Scenario: 参数缺失返回 -32602
- **WHEN** 客户端 `tools/call(name="run_skill_turn", arguments={"skill_id": "x"})`（缺 message）
- **THEN** server SHALL 返回 JSON-RPC error，code=-32602，message 含 `"message"` 字样

### Requirement: resources/list 暴露所有 skills 为资源

`resources/list` SHALL 遍历 `pool.skill_registry.snapshot()`，每个 skill 返回：

```json
{
  "uri": "taifeng://skill/<skill_id>",
  "name": <skill.name>,
  "description": <skill.description>,
  "mimeType": "text/markdown"
}
```

#### Scenario: 2 个 skill 都列出
- **WHEN** registry 加载了 `style-checker` 与 `code-reviewer` 两个 skill
- **AND** 客户端调 `resources/list`
- **THEN** result.resources SHALL 含恰好 2 项
- **AND** 两项的 uri SHALL 等于 `"taifeng://skill/style-checker"` 与 `"taifeng://skill/code-reviewer"`

### Requirement: resources/read 返回 SKILL.md body

`resources/read(uri)` SHALL：

1. 校验 uri 形如 `taifeng://skill/<skill_id>`；非此 scheme → `-32602 Invalid params`
2. 从 snapshot 取 `SkillDefinition`；未知 → `-32602 Invalid params`，message 含 skill_id
3. 返回 `{"contents": [{"uri": uri, "mimeType": "text/markdown", "text": defn.body}]}`

#### Scenario: 读已知 skill body
- **WHEN** 客户端 `resources/read(uri="taifeng://skill/style-checker")`
- **THEN** result.contents[0].text SHALL 等于该 skill 的 body
- **AND** result.contents[0].mimeType SHALL 等于 `"text/markdown"`

#### Scenario: 读未知 URI 返回 invalid params
- **WHEN** 客户端 `resources/read(uri="taifeng://skill/ghost")`
- **THEN** server SHALL 返回 JSON-RPC error code=-32602
- **AND** error.message SHALL 含 `"ghost"`

#### Scenario: 拒绝非 taifeng scheme
- **WHEN** 客户端 `resources/read(uri="file:///etc/passwd")`
- **THEN** server SHALL 返回 -32602 error

### Requirement: CLI `mcp serve` 子命令

`python -m taifeng mcp serve <skills_dir> --storage <dir> [--model <model>] [--enable-hitl]` SHALL：

1. 加载 skills_dir + 构造默认 `EnginePool`（用 `LiteLLMClient(model=args.model)` 作为 model_client，默认 model `"gpt-4o-mini"`）
2. **当 `--enable-hitl` 设置时**：构造 `McpPrompter(server)` + `PermissionPolicy(prompter=prompter, default_mode="ask")`，注入 `EnginePool.create(permission_policy=...)`
3. 实例化 `McpStdioServer(pool)` 并 `asyncio.run(server.run())`
4. 错误（skills_dir 不存在 / storage 不可写）SHALL 打印到 stderr + 进程 exit code 1
5. SHALL NOT 向 stdout 输出非 JSON-RPC 内容（防止协议流被污染）

#### Scenario: --help 显示子命令文档
- **WHEN** 用户运行 `python -m taifeng mcp serve --help`
- **THEN** stdout SHALL 含 `skills_dir` / `--storage` / `--model` / `--enable-hitl` 四个参数说明
- **AND** exit code SHALL 等于 0

#### Scenario: --enable-hitl 注入 McpPrompter
- **WHEN** 用户运行 `python -m taifeng mcp serve <skills> --storage <s> --enable-hitl`
- **THEN** EnginePool SHALL 用 `PermissionPolicy(prompter=<McpPrompter instance>, default_mode="ask")` 构造
- **AND** McpPrompter 持有 server 引用（共享 stdin/stdout）

### Requirement: JSON-RPC 错误处理

未知 method SHALL 返回 `-32601 Method not found`；JSON 解析失败 SHALL 返回 `-32700 Parse error`；params 非法 SHALL 返回 `-32602 Invalid params`。错误响应符合 JSON-RPC 2.0 schema：

```json
{"jsonrpc": "2.0", "id": <req_id_or_null>, "error": {"code": <code>, "message": <str>}}
```

#### Scenario: 未知 method
- **WHEN** 客户端发 `{"method": "tools/explode", ...}`
- **THEN** server SHALL 返回 error code=-32601

#### Scenario: parse error 时 id=null
- **WHEN** stdin 收到 `not-json-at-all\n`
- **THEN** server SHALL 返回 `{"error": {"code": -32700, ...}, "id": null}`

### Requirement: Server-initiated request 双向 JSON-RPC

`McpStdioServer` SHALL 提供 `async server_initiated_request(method: str, params: dict, *, timeout: float = 60.0) -> dict` 方法，让 server 端可发起 client-bound JSON-RPC 请求并等响应。

实现 SHALL：

1. 分配单调递增的 outgoing id，格式 `srv_<N>`（N 从 1 起），与 client 发起的 id 命名空间隔离
2. 把请求 `{"jsonrpc":"2.0","id":"srv_<N>","method":<method>,"params":<params>}` 写到 stdout（过 `_write_lock`）
3. 创建 `asyncio.Future`，存到 `self._pending_outgoing[id]`
4. `await asyncio.wait_for(future, timeout)` 等待 client 回包
5. 超时 SHALL 抛 `TimeoutError`，**并从 `_pending_outgoing` 清理 future**

#### Scenario: outgoing request id 用 srv_ 前缀
- **WHEN** server 调 `server_initiated_request("elicitation/create", {...})`
- **AND** 此前已派发过 client 请求 id=1
- **THEN** outgoing 请求的 id SHALL 以 `"srv_"` 开头（如 `"srv_1"`）
- **AND** 与 client id=1 不冲突

#### Scenario: 收到 client response 时 resolve future
- **WHEN** server 写出 `{"id":"srv_1", ...}` 后等待
- **AND** stdin 收到 `{"jsonrpc":"2.0","id":"srv_1","result":{"action":"accept","content":{"approved":true}}}`
- **THEN** `server_initiated_request` 的 await SHALL 返回该 result dict
- **AND** `_pending_outgoing["srv_1"]` SHALL 被移除

#### Scenario: outgoing request 超时
- **WHEN** server 调 `server_initiated_request(..., timeout=0.1)` 且 client 不回
- **THEN** SHALL 抛 `TimeoutError`
- **AND** `_pending_outgoing` SHALL 不再含该 id
- **AND** 后续 stdin 若收到该 id 的迟到 response，SHALL 仅 log warning 不崩溃

### Requirement: stdin 路由区分 incoming request / incoming response

`McpStdioServer._handle_line` SHALL 区分三种 incoming JSON-RPC 消息：

1. `{"id": <x>, "method": <m>, ...}` → incoming request（client → server）→ 走既有 `_dispatch`
2. `{"id": <x>, "result"|"error": ..., 没有 method}` → incoming response（client 对 server-initiated request 的回包）→ pop `self._pending_outgoing[x]` 并 `set_result(result)` 或 `set_exception(McpError(error))`
3. `{"method": <m>, ...}` 无 id → notification → 既有 no-response 路径

#### Scenario: incoming response 路由到 pending future
- **WHEN** stdin 收到 `{"jsonrpc":"2.0","id":"srv_1","result":{...}}`
- **AND** `_pending_outgoing["srv_1"]` 存在
- **THEN** server SHALL NOT 调 `_dispatch`
- **AND** future SHALL 被 `set_result(result_dict)`

#### Scenario: 孤儿 response（无对应 pending future）
- **WHEN** stdin 收到 `{"jsonrpc":"2.0","id":"srv_99","result":{...}}` 但 `_pending_outgoing["srv_99"]` 不存在
- **THEN** server SHALL log warning（含 id 与 result 前 100 字）
- **AND** SHALL NOT 崩溃 / 中断主循环

### Requirement: stdout 写入并发互斥

`McpStdioServer` SHALL 持有 `_write_lock: asyncio.Lock`，所有写 stdout 操作（无论 response of incoming request、还是 server-initiated outgoing request）SHALL 经过该锁，保证并发 turn 与并发 elicitation 不会写交错。

实现 SHALL 抽出 `async def _write_message(payload: dict) -> None` 内部方法，统一加锁 + JSON encode + drain。

#### Scenario: 两次并发 server_initiated_request 不写交错
- **WHEN** 两个 turn 同时触发 elicitation
- **AND** 两次 `server_initiated_request` 并发起动
- **THEN** stdout 上 SHALL 看到完整 2 个 JSON 对象（按行分隔）
- **AND** SHALL NOT 出现一个 JSON 内嵌入另一个 JSON 字符的情况

### Requirement: server_initiated_request 可取消

`server_initiated_request` SHALL 接受可选 `cancel: CancellationToken | None = None` 参数：

- `cancel.cancel()` 触发时，pending future SHALL `cancel()`
- 同时 `_pending_outgoing` SHALL 清理该 id
- 调用方 await SHALL 抛 `CancelledError`

#### Scenario: cancellation token 触发清理
- **WHEN** turn 启动了 elicitation，await 期间 cancel.cancel() 被触发
- **THEN** server_initiated_request 的 await SHALL 抛 CancelledError
- **AND** `_pending_outgoing` SHALL 不再含该 id
- **AND** 之后 stdin 若收到该 id 的 response，仅 log warning

### Requirement: Telemetry 事件覆盖 elicitation 生命周期

`McpStdioServer` SHALL 在 `server_initiated_request` 内部 emit 3 个新 EventMsg kind：

- `elicitation_started`：`data = {"method": <str>, "id": <str>, "params_preview": <truncated str ≤200>}`
- `elicitation_completed`：`data = {"method": <str>, "id": <str>, "duration_ms": <int>, "outcome": "ok|timeout|cancelled|error"}`
- `elicitation_timed_out`：`data = {"method": <str>, "id": <str>, "timeout": <float>}`

业务侧通过构造 `McpStdioServer(pool, emit=<async callable>)` 注入 emit 回调；为 `None` 时 SHALL 走 logger.info（不 raise）。

#### Scenario: 成功 elicitation emit started + completed
- **WHEN** elicitation 成功完成
- **THEN** SHALL 按时序 emit `elicitation_started` → `elicitation_completed(outcome="ok", duration_ms=<>)`

#### Scenario: 超时 emit started + timed_out + completed(outcome=timeout)
- **WHEN** elicitation 超时
- **THEN** SHALL emit `elicitation_started` → `elicitation_timed_out` → `elicitation_completed(outcome="timeout")`

