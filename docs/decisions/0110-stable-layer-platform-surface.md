# ADR 0110：稳定层补齐平台对接面

- 状态：Accepted
- 日期：2026-09-30
- 关联：Amends #0066；[public-api](../architecture/public-api.md)、[mcp-client](../architecture/capabilities/mcp-client.md)、
  [dynamic-tool-set](../architecture/capabilities/dynamic-tool-set.md)

## 背景

ADR 0066 把顶层 `__all__` 定为稳定层，下游（平台与适配包）据此立了规矩：只 import 稳定层。落实时
发现稳定层自己不自洽，下游照规矩走不通：

1. **稳定协议的签名里有没导出的类型**。`MessageStore.list_threads` 返回 `ThreadInfo`，
   `ModelClient.session` 返回 `ModelClientSession`，`CompressionStrategy` 的方法收 `CompressionTrigger`——
   协议公开了，实现它要用的类型却只能从内部模块拿。
2. **稳定入口的参数类型没导出**。`EnginePool.create(retry_config=, input_cost_estimator=, sink=)` 要调用方
   构造 `RetryConfig`、实现 `InputCostEstimator` / `TelemetrySink`；`McpStdioClient.spawn(elicitation_handler=)`
   要 `ElicitationHandler`。Responses 协议的模型客户端要求 store 实现 `AtomicBatchMessageStore`，它与
   `BatchAppendAck` / `BatchConflictError` 都没导出，外部 `MessageStore` 因此只能配 Chat 协议。
3. **成熟的能力没有稳定入口**。内置工具只导出了 7 个工厂，`shell_exec`、`file_read` / `file_write`、
   `http_request`、`glob` / `grep`、`memory`、`spawn_skill` 一族、`request_user_input` 以及 `create` 默认
   注册的四个都不在；`CancelTurn` 不在（稳定层的操作只有 `UserMessage` / `Resume` / `SendToPeer` /
   `UpdateInstructions`，下游只能关掉整个会话来取消一轮）；模拟器 `SimClient` 不在（下游写测试只能
   自己造一个脚本化客户端）；`TelemetrySink` 与两个内置 sink 不在。
4. **MCP 的 HTTP 传输与工具集绑定停在实验层**。它们的契约标 🧪 的理由是只对着内核自带的假 server 与
   httpx `MockTransport` 测过——能证明内核按自己对规范的理解工作，证明不了与别人的实现互通。

本轮由项目负责人明确要求补齐（平台对接缺口清单第 3、4、6、8、9、10、12 项与第 2 项的一半）。

## 决策

1. **规则：稳定协议公开方法签名里的 taifeng 类，必须在公共 API 里。** 协议公开即承诺了它的输入输出
   形状。`tests/test_public_api.py::test_stable_protocol_signatures_only_use_exported_types` 按源码里的
   注解逐个协议核对，今后新增协议或改签名漏了导出，测试即红。
2. **晋升到稳定层（49 个名字）**：

   | 类别 | 名字 |
   | --- | --- |
   | 会话存储 | `ThreadInfo`、`AtomicBatchMessageStore`、`BatchAppendAck`、`BatchConflictError` |
   | 压缩 | `CompressionTrigger` |
   | 模型客户端 | `ModelClientSession`、`ModelCapabilities`、`RetryConfig`、`InputCostEstimator` |
   | 模拟器 | `SimClient`、`SimTurn`、`RoutingSimClient`、`SimFault`、`SimExpect`、`SimContractViolation`、`SimScriptExhausted` |
   | 操作 | `CancelTurn` |
   | 遥测 | `TelemetrySink`、`ConsoleSink`、`JsonlSink`、`attach_console_sink`、`attach_jsonl_sink` |
   | 内置工具工厂 | `make_shell_exec_tool`、`make_file_read_tool`、`make_file_write_tool`、`make_http_request_tool`、`make_glob_tool`、`make_grep_tool`、`make_memory_tool`、`make_request_user_input_tool`、`make_spawn_skill_tool`、`make_await_skills_tool`、`make_join_skill_tool`、`make_kill_skill_tool`、`make_read_skill_tool`、`make_call_skill_tool`、`make_run_script_tool`、`make_search_skills_tool` |
   | MCP | `McpClient`、`McpHttpClient`、`McpToolBinding`、`bind_mcp_tools`、`register_mcp_tools_async`、`ElicitationHandler`、`ElicitationRequest`、`ElicitationResult`、`McpProtocolVersionError`、`McpPaginationError`、`McpContentError` |

3. **`mcp-client` 与 `dynamic-tool-set` 两份契约转 ✅**，依据是与官方 MCP Python SDK 写的 server 互通
   （`examples/mcp_interop/`，SDK 2.2.0）：真实 stdio 子进程与真实 HTTP 连接各跑一遍，协议版本协商
   （SDK 最新版本是 2026-07-28，协商到内核声明的 2025-06-18）、`tools/list`、文本与结构化结果、server
   拒绝参数、`bind_mcp_tools` 注册后经 handler 调用、运行期 `tools/list_changed` 后自动同步，22 项全部
   通过。
4. **晋升的实验层名字保留一个发布版本**。`McpHttpClient` / `McpToolBinding` / `bind_mcp_tools` 从
   `taifeng.experimental.__all__` 移除，模块 `__getattr__` 照常返回对象并发 `DeprecationWarning` 提示改从
   顶层导入。

## 不做

- **导出同步版 `register_mcp_tools`**：它早已作废，调用即抛 `RuntimeError`（`tools/list` 是网络调用）。
  缺口清单点名的是它，实际该用的是 `register_mcp_tools_async`。
- **把稳定层具体类的签名类型全部导出**。对稳定层全部类与函数做同样的核对还有约 40 个未导出的名字
  （`ToolSpecRef`、`CacheBreakpoint`、`CallFrame`、各 provider 的 session 类等），多数是调用方只读不构造的
  数据类型或字面量别名。规则 1 只管「外部要实现的协议」；其余按需再议。
- **其余 provider 客户端（`AnthropicClient`、`GeminiClient`、`DeepSeekClient`、`LiteLLMClient`、
  `OpenAICompatClient`）**：不在这轮缺口里；Anthropic 的真实端点验证仍缺。
- **实现 MCP 2026-07-28 的 `subscriptions/listen`**：互通验证里参照 server 对旧版本客户端发的是
  `notifications/tools/list_changed`；新机制等内核升级声明的协议版本时再做。

## 影响

- R1–R5：无行为变化，只改导出。
- 稳定层从 160 个名字增到 209 个；这些名字今后的不兼容修改要走 ADR 0066 的弃用流程。
- 下游包里为绕开缺口写的单点引入（如只为 `ThreadInfo` 开的内部 import）可以删掉。

## 验证

- `tests/test_public_api.py`（53 项）：快照一致；稳定协议签名类型全部已导出；对接面名字逐个在稳定层；
  晋升的三个名字从实验层仍可取到并告警；只用稳定层名字起一个模拟会话、开文件工具、跑完一轮、
  发 `CancelTurn`、列出 `ThreadInfo`。
- `PYTHONPATH=src uv run --with mcp python examples/mcp_interop/verify.py`：22 项通过（2026-09-30，
  mcp 2.2.0）。
