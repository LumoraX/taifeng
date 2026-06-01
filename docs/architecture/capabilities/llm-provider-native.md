# llm-provider-native Specification

## Purpose
TBD - created by archiving change native-provider-pack. Update Purpose after archive.
## Requirements
### Requirement: native provider 四件套同构契约

`src/taifeng/llm/providers/` 下的四个 native client（`OpenAICompatClient` / `AnthropicClient` / `GeminiClient` / `DeepSeekClient`）SHALL 全部实现 `ModelClient` 协议，包含：

- `session(*, cancel: CancellationToken, model: str | None = None) -> Session` 方法
- `record_cache_read(value: int) -> None` 方法
- 对应的 `Session.stream(request: ApiRequest) -> AsyncIterator[ResponseEvent]` 方法

四家 session SHALL emit **同形状** 的 `ResponseEvent` 序列：

```
created → server_model → (text_delta | reasoning_delta | tool_call_delta)*
       → (tool_call_done)* → (prompt_cache)? → completed
```

错误路径 SHALL emit `error(...)` 事件并 raise 对应 `LLMError` 子类。

#### Scenario: ModelClient 协议契合
- **WHEN** 任意 native client 被赋给 `Engine(model_client=...)`
- **THEN** Engine 调用 `client.session(cancel=..., model=...)` SHALL 返回带 `.stream()` async generator 方法的对象
- **AND** session 实例 SHALL 实现 `__aenter__` / `__aexit__` async context manager 协议

#### Scenario: ResponseEvent 流形状一致
- **WHEN** 四家 native client 各自跑一次最小 turn（system + user message，无 tool）
- **THEN** 四家 emit 的事件序列首尾 SHALL 为 `created` 起 / `completed` 终
- **AND** `completed.usage.input_tokens` / `output_tokens` SHALL 来自上游 response 的真实 usage（不是 0）

---

### Requirement: AnthropicClient 走 native messages API

`AnthropicClient` SHALL 通过 httpx 直连 `https://api.anthropic.com/v1/messages`，使用 SSE 流式响应，不依赖 `anthropic-sdk-python` 包。

请求 SHALL 满足：

- HTTP method `POST`，header `x-api-key: <key>` + `anthropic-version: 2023-06-01` + `content-type: application/json`
- body 字段 `model` / `system` / `messages` / `tools` / `max_tokens`（**Anthropic 必填**）/ `temperature`?
- `tools` 字段格式 `[{name, description, input_schema}]`（注意是 `input_schema` 不是 `parameters`）
- `messages` 中 assistant role 的 tool_calls SHALL 翻译为 `content: [{type: "tool_use", id, name, input}]`
- `messages` 中 tool role 的 result SHALL 翻译为 `user` role + `content: [{type: "tool_result", tool_use_id, content}]`
- `cache_breakpoints` SHALL 翻译为对应 content block 的 `cache_control: {type: "ephemeral"}` 字段

SSE 事件 SHALL 按 Anthropic `event: <type>\ndata: {...}` 双行格式解析：

| 上游 event | Taifeng 事件 |
| --- | --- |
| `message_start` | `created()` + `server_model(model)` |
| `content_block_delta`（delta.type=`text_delta`）| `text_delta(delta.text)` |
| `content_block_delta`（delta.type=`input_json_delta`） | `tool_call_delta(call_id, name, partial_json)` |
| `content_block_start`（type=`tool_use`） | 记录 tool call id+name，不发事件 |
| `message_delta`（含 usage） | 更新 `_last_usage` |
| `message_stop` | `prompt_cache(...)` + `completed(end_turn=stop_reason in {"end_turn","stop_sequence"})` |

#### Scenario: 最小文本 turn
- **WHEN** `AnthropicSession.stream(ApiRequest(messages=[user="hi"]))` 被消费
- **AND** 上游 SSE 返回 `message_start` → `content_block_start{text}` → 多个 `content_block_delta{text_delta}` → `message_delta` → `message_stop`
- **THEN** session SHALL 依次 yield `created` → `server_model` → 多个 `text_delta` → `prompt_cache` → `completed(end_turn=True)`

#### Scenario: tool_use 流式
- **WHEN** 上游 SSE 含 `content_block_start{type: "tool_use", id: "tu_X", name: "search"}` 后跟多个 `content_block_delta{input_json_delta: "{\"q\":"}` / `delta{"hi\"}"}`
- **THEN** session SHALL yield `tool_call_delta(call_id="tu_X", name="search", delta="{\"q\":")` + `tool_call_delta(call_id="tu_X", name="search", delta="hi\"}")`
- **AND** 流末 yield `tool_call_done(call_id="tu_X", name="search", arguments="{\"q\":\"hi\"}")` + `completed(end_turn=False)`

#### Scenario: cache_breakpoints 注入 cache_control
- **WHEN** `ApiRequest.cache_breakpoints` 含一个指向 `messages[0]` 的 breakpoint
- **THEN** 实际发往 Anthropic 的 body 中 `messages[0].content[-1]` SHALL 含 `cache_control: {type: "ephemeral"}`

#### Scenario: cache 元数据从 message_start 直接取
- **WHEN** Anthropic 返回的 `message_start.message.usage` 含 `cache_creation_input_tokens: 100` + `cache_read_input_tokens: 200`
- **THEN** session 末尾 emit 的 `prompt_cache` 事件 SHALL `cache_creation=100` + `cache_read=200`
- **AND** `completed.usage.cache_creation_input_tokens == 100`，`completed.usage.cache_read_input_tokens == 200`

---

### Requirement: GeminiClient 走 streamGenerateContent SSE

`GeminiClient` SHALL 通过 httpx 直连 `https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse`，使用 SSE 流式响应，不依赖 `google-generativeai` 包。

请求 SHALL 满足：

- HTTP method `POST`，认证通过 query 参数 `?key={api_key}` 或 header `x-goog-api-key`
- body 字段 `contents` / `systemInstruction` / `tools` / `generationConfig`
- `contents` 中 role 映射：`user` → `user`，`assistant` → `model`，`tool` → `function`（**Gemini 用 function 而非 tool**）
- `tools` 字段格式 `[{functionDeclarations: [{name, description, parameters}]}]`
- `system_prompt: list[str]` 合并为 `systemInstruction: {parts: [{text: "<合并文本>"}]}`

SSE 事件 SHALL 按 Gemini `data: {...}\n\n` 单行格式解析：

| 上游 chunk 字段 | Taifeng 事件 |
| --- | --- |
| 首 chunk 的 `modelVersion` 或 config 中的 model | `created()` + `server_model(model)` |
| `candidates[0].content.parts[].text` | `text_delta(text)` |
| `candidates[0].content.parts[].functionCall` | `tool_call_done(call_id=auto, name, arguments=json.dumps(args))` —— Gemini 不流式发 tool args delta |
| `candidates[0].finishReason` | `STOP` → end_turn=True；`TOOL_CALL` / `MAX_TOKENS` → end_turn=False |
| 末 chunk 的 `usageMetadata` | 更新 `_last_usage` + emit `prompt_cache` |

#### Scenario: 最小文本 turn
- **WHEN** `GeminiSession.stream(ApiRequest(messages=[user="hi"]))` 被消费
- **AND** 上游 SSE 返回多个 chunk 各带 `candidates[0].content.parts[0].text`，末 chunk 带 `usageMetadata`
- **THEN** session SHALL yield `created` → `server_model` → 多个 `text_delta` → `prompt_cache` → `completed(end_turn=True)`

#### Scenario: functionCall 整体到达
- **WHEN** 上游 SSE chunk 含 `candidates[0].content.parts[0].functionCall: {name: "search", args: {"q": "hi"}}`
- **THEN** session SHALL 不 emit `tool_call_delta`
- **AND** 流末 yield 单个 `tool_call_done(call_id=<auto>, name="search", arguments="{\"q\":\"hi\"}")` + `completed(end_turn=False)`

#### Scenario: cache 元数据从 usageMetadata 取
- **WHEN** Gemini 返回 `usageMetadata: {promptTokenCount: 500, candidatesTokenCount: 100, cachedContentTokenCount: 200}`
- **THEN** `completed.usage.input_tokens == 500` + `output_tokens == 100` + `cache_read_input_tokens == 200`

#### Scenario: cancel 中断流
- **WHEN** session 在消费 SSE 中途收到 `CancellationToken.cancel()`
- **THEN** 下一个 chunk 处理时 SHALL raise `CancelledError`
- **AND** httpx response stream SHALL 被关闭（无连接泄漏）

---

### Requirement: 共享 `_shared.py` 提供错误分类与 SSE 解析

`src/taifeng/llm/providers/_shared.py` SHALL 导出：

- `classify_http_error(status: int, body: str, *, provider: str = "openai") -> LLMError`
- `parse_sse_data(line: str) -> dict | None` —— SSE `data: <json>` 单行解析
- `parse_sse_event(lines: list[str]) -> tuple[str | None, dict | None]` —— Anthropic 风格 `event: <name>\ndata: <json>` 双行解析

错误分类规则 SHALL 优先按 HTTP status code：

| 输入 | 输出 |
| --- | --- |
| `status in (401, 403)` | `AuthenticationError(body)` |
| `status == 429` | `RateLimitError(body, retry_after_seconds=<解析自 body 或 Retry-After header>)` |
| `status == 408` | `TransientNetworkError(body)` |
| `status >= 500` | `ServerError(body)` |
| `status == 400 + body 含 "context_length"/"too long"/"maximum tokens"` | `ContextOverflowError(body)` |
| `status == 400 + body 含 "safety"/"content_filter"/"blocked"` | `ContentFilterError(body)` |
| `其他 4xx` | `InvalidRequestError(body)` |

#### Scenario: classify 401
- **WHEN** `classify_http_error(401, '{"error":{"message":"invalid api key"}}')`
- **THEN** 返回 `AuthenticationError` 实例

#### Scenario: classify 429 with retry_after
- **WHEN** `classify_http_error(429, '{"error":{"type":"rate_limit","retry_after":30}}')`
- **THEN** 返回 `RateLimitError` 实例，且 `retry_after_seconds == 30`

#### Scenario: classify 500
- **WHEN** `classify_http_error(500, 'upstream error')`
- **THEN** 返回 `ServerError` 实例，且 `.retryable == True`

#### Scenario: classify 400 context overflow
- **WHEN** `classify_http_error(400, "prompt is too long: maximum tokens is 200000")`
- **THEN** 返回 `ContextOverflowError` 实例

#### Scenario: parse SSE data 行
- **WHEN** `parse_sse_data('data: {"x":1}')`
- **THEN** 返回 `{"x": 1}`

#### Scenario: parse SSE [DONE] 标记
- **WHEN** `parse_sse_data('data: [DONE]')`
- **THEN** 返回 `None`（表示流结束）

#### Scenario: parse Anthropic event 双行
- **WHEN** `parse_sse_event(['event: message_start', 'data: {"type":"message_start"}'])`
- **THEN** 返回 `("message_start", {"type": "message_start"})`

---

### Requirement: DeepSeekClient 作为 OpenAICompatClient 薄子类

`DeepSeekClient` SHALL 是 `OpenAICompatClient` 的子类，**不重新实现** SSE 解析 / payload 构造 / tool_calls 累积逻辑（DeepSeek API 与 OpenAI chat/completions 100% 兼容）。

`DeepSeekClient` SHALL 满足：

- 构造签名：`DeepSeekClient(*, api_key, model="deepseek-chat", base_url="https://api.deepseek.com", extra_headers=None, timeout_seconds=300.0)`
- `model="deepseek-reasoner"` 时，复用 `OpenAICompatSession._process_chunk` 现有的 `reasoning_content` 处理路径（emit `reasoning_delta`），**无需 R1 专属代码**
- usage 字段提取 SHALL 通过 `_shared.extract_usage_openai_family(raw)` 支持 DeepSeek 特有字段 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，映射到 `TokenUsage.cache_read_input_tokens`
- `_shared.extract_usage_openai_family` SHALL 按以下优先级查找 cache_read：
  1. `raw["cache_read_input_tokens"]`（Anthropic 风格 / 部分 OpenAI 版本）
  2. `raw["prompt_tokens_details"]["cached_tokens"]`（OpenAI 标准）
  3. `raw["prompt_cache_hit_tokens"]`（DeepSeek）

#### Scenario: 默认 base_url 与 model
- **WHEN** `DeepSeekClient(api_key="sk-xxx")` 构造
- **THEN** 内部 `base_url == "https://api.deepseek.com"`，默认 `model == "deepseek-chat"`

#### Scenario: 复用 OpenAICompat 流式逻辑
- **WHEN** `DeepSeekClient(api_key="...").session(cancel=...).stream(req)` 被消费
- **AND** 上游返回标准 OpenAI chat/completions SSE 响应
- **THEN** session SHALL 产生与 `OpenAICompatSession` 等价的 `ResponseEvent` 流（`created` → `server_model` → `text_delta*` → ...）

#### Scenario: DeepSeek cache 字段提取
- **WHEN** 上游 chunk 含 `usage: {prompt_tokens: 1000, completion_tokens: 200, prompt_cache_hit_tokens: 800, prompt_cache_miss_tokens: 200}`
- **THEN** `_last_usage.cache_read_input_tokens == 800`
- **AND** `completed.usage.input_tokens == 1000`，`completed.usage.output_tokens == 200`

#### Scenario: R1 推理模式发 reasoning_delta
- **WHEN** `DeepSeekClient(api_key="...", model="deepseek-reasoner")` session 消费包含 `delta.reasoning_content` 的 SSE chunk
- **THEN** SHALL emit `reasoning_delta(content)` 事件
- **AND** 之后的 `delta.content` SHALL 正常 emit `text_delta`

#### Scenario: extract_usage_openai_family 优先级（OpenAI 标准字段）
- **WHEN** `extract_usage_openai_family({"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 50}})`
- **THEN** 返回 `TokenUsage` 实例，`cache_read_input_tokens == 50`

#### Scenario: extract_usage_openai_family 优先级（DeepSeek 字段）
- **WHEN** `extract_usage_openai_family({"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 80})`
- **THEN** 返回 `TokenUsage` 实例，`cache_read_input_tokens == 80`

---

### Requirement: 网络异常映射到 TransientNetworkError

四家 native client SHALL 在捕获 httpx 网络异常时直接 raise `TransientNetworkError`，使其归入 `retryable_kinds`：

| httpx 异常 | Taifeng 异常 |
| --- | --- |
| `httpx.ConnectError` | `TransientNetworkError` |
| `httpx.ConnectTimeout` | `TransientNetworkError` |
| `httpx.ReadTimeout` | `TransientNetworkError` |
| `httpx.WriteTimeout` | `TransientNetworkError` |
| `httpx.RemoteProtocolError` | `TransientNetworkError` |
| `httpx.PoolTimeout` | `TransientNetworkError` |

#### Scenario: 连接拒绝归为 transient
- **WHEN** httpx 抛 `ConnectError("Connection refused")`
- **THEN** native client SHALL raise `TransientNetworkError`，且 `.kind == "transient_network"` 且 `.retryable == True`

#### Scenario: 读超时归为 transient
- **WHEN** httpx 抛 `ReadTimeout`
- **THEN** native client SHALL raise `TransientNetworkError`，可被 `retry_async` 重试

---

### Requirement: 空 api_key 时省略鉴权头/参数（支持本地无鉴权端点）

native client SHALL 在 `api_key` 为空或纯空白时**省略**鉴权头/参数，而非发出语义为空的凭据。动机有二：

1. **本地无鉴权端点**（Ollama / LM Studio / vLLM 等 OpenAI 兼容服务）本就不需要 key —— 必须能在不配 key 的情况下正常调用。
2. **避免非法 header**：旧实现对空 key 发出 `Authorization: Bearer `（带尾空格），httpx 在请求构造阶段即抛 `LocalProtocolError: Illegal header value`，被分类为 `failure_class=unknown` —— 报错隐晦且无法重试。省略后，对真实需鉴权的服务端会收到干净的 401 → 分类为清晰的 `AuthenticationError`（`provider_auth`）。

各 client 的省略规则：

| client | 鉴权方式 | 空 key 行为 |
| --- | --- | --- |
| `OpenAICompatClient` / `DeepSeekClient` | header `Authorization: Bearer {key}` | 省略 `Authorization` 头 |
| `AnthropicClient` | header `x-api-key: {key}` | 省略 `x-api-key` 头 |
| `GeminiClient`（`auth_via="header"`） | header `x-goog-api-key: {key}` | 省略 `x-goog-api-key` 头 |
| `GeminiClient`（`auth_via="query"`） | URL `&key={key}` | URL 不挂 `&key=` |

`extra_headers` 在鉴权头之后合并 —— 即便 `api_key` 为空，业务侧仍可通过 `extra_headers` 注入网关自定义鉴权头。

> **设计对齐**：此规则与官方 OpenAI Python SDK 的 `auth_headers` 同语义（`if not api_key: return {}` —— 空字符串会导致 header 编码失败，故省略）。**授权有效性由服务端裁决，client 不本地预判**：无鉴权端点正常工作，需鉴权端点缺头则 401。决策与动机见 ADR `docs/decisions/0011-empty-api-key-omits-auth.md`。

#### Scenario: 空 key 省略 Authorization（本地 Ollama）
- **WHEN** `OpenAICompatClient(api_key="").session(...)` 构造
- **THEN** session `_headers` SHALL NOT 含 `Authorization`，且对本地无鉴权端点正常发请求（不抛 `LocalProtocolError`）

#### Scenario: 空 key 连需鉴权端点 → 干净 401（非 LocalProtocolError）
- **WHEN** `api_key=""` 连真实需鉴权的服务端（无 `extra_headers` 鉴权）并消费 stream
- **THEN** 服务端因缺鉴权头返回 401 → `classify_http_error` 归为 `AuthenticationError`（`failure_class=provider_auth`），**而非** httpx `LocalProtocolError`/`failure_class=unknown`

#### Scenario: 纯空白 key 同样视为无 key
- **WHEN** `api_key="   "`（仅空白）
- **THEN** 各 client SHALL 省略对应鉴权头/参数

#### Scenario: extra_headers 可在空 key 下注入鉴权
- **WHEN** `OpenAICompatClient(api_key="", extra_headers={"Authorization": "Custom t"})`
- **THEN** session `_headers["Authorization"] == "Custom t"`

---

### Requirement: LiteLLM 作为非主流 provider 兜底，文档明确选型

`LiteLLMClient` SHALL 保留，**不删除**。`docs/architecture/overview.md`（或 `docs/architecture/llm-client.md` 若新建）SHALL 包含选型表：

| 场景 | 推荐 client |
| --- | --- |
| OpenAI / 自部署 OpenAI-compat gateway (vLLM, Ollama, one-api) | `OpenAICompatClient` |
| Anthropic Claude API（含 messages 流式 / cache_control） | `AnthropicClient` |
| Google Gemini API（AI Studio） | `GeminiClient` |
| DeepSeek（V3 / R1，含 prompt cache） | `DeepSeekClient` |
| AWS Bedrock / GCP Vertex / Azure OpenAI / 其他自定义 endpoint | `LiteLLMClient` |

`docs/configurable-knobs.md` SHALL 列出新增的构造参数：

- `AnthropicClient.api_key` / `.model` / `.base_url`（默认 `https://api.anthropic.com`） / `.extra_headers` / `.timeout_seconds`
- `GeminiClient.api_key` / `.model` / `.base_url`（默认 `https://generativelanguage.googleapis.com`） / `.auth_via`（`"query"` / `"header"`） / `.timeout_seconds`
- `DeepSeekClient.api_key` / `.model`（默认 `deepseek-chat`，可选 `deepseek-reasoner`） / `.base_url`（默认 `https://api.deepseek.com`） / `.extra_headers` / `.timeout_seconds`

#### Scenario: docs 含四家 native client 选型表
- **WHEN** 读 `docs/architecture/overview.md` 的 LLM 章节
- **THEN** SHALL 看到 native vs LiteLLM 的选型表，含覆盖矩阵（4 行 native + 1 行 LiteLLM 兜底）

#### Scenario: configurable-knobs 列出 native client 参数
- **WHEN** 读 `docs/configurable-knobs.md`
- **THEN** SHALL 看到 `AnthropicClient` + `GeminiClient` + `DeepSeekClient` 三家的构造时参数清单

### Requirement: 异常终止（finish_reason）暴露为错误

`openai_compat` provider 在流末 SHALL 检查 `choices[].finish_reason`。当 `finish_reason=content_filter`（模型/网关主动拦截，返回空 content）且本次流未累积任何 tool call 时，provider SHALL：

1. 先 emit 一个 `error` 事件（`kind="content_filter"`、`retryable=False`），与 HTTP 错误路径一致；
2. 再抛 `ContentFilterError`（回填服务端 `request_id`）；
3. **不得** emit `completed` 事件（不把被拦截伪造成功）。

正常终止（`finish_reason=stop` / `tool_calls` / 无 finish_reason 但有内容）SHALL 不受影响，照常 emit `completed`。

#### Scenario: content_filter 空流抛 ContentFilterError
- **WHEN** provider 收到 `finish_reason=content_filter` + 空 content + 0 token 的流
- **THEN** SHALL emit `error{kind=content_filter}` 事件并抛 `ContentFilterError`，且不 emit `completed`

#### Scenario: 正常 stop 不受影响
- **WHEN** provider 收到 `finish_reason=stop` + 非空 content 的流
- **THEN** SHALL 照常 emit `text_delta` + `completed`，不抛异常

### Requirement: 无显式错误的空 completion 视为正常完成（loop 层不臆断）

判据：**只有 LLM 显式报错才是错误；模型没产出内容本身不是错误。** turn loop 在某轮采样无 tool call 时，即按正常终止处理——即便该 turn 无任何文本产出。此时 turn SHALL `success=True`、`final_text=""`，`call_skill` SHALL 回 `ToolResult.ok("")`，父 turn 拿到空结果继续。

loop 层 SHALL NOT 仅因「输出为空」就臆断为异常（空可能源于 prompt / skill，归因属业务侧）。只有 LLM **显式信号**（provider 上报的 `finish_reason=content_filter`、`error` 事件、非 200 状态）才使 turn 判失败。

#### Scenario: 空子 turn 被容忍、任务继续
- **WHEN** 子 skill 的 turn 返回空（无 text + 无 tool call、无显式错误）
- **THEN** 该 turn `success` SHALL 为 True，`call_skill` 结果 SHALL `is_error=false`（ok 空结果），父 turn SHALL 继续

#### Scenario: 显式 content_filter 才判错
- **WHEN** 子 skill 的 turn 因 `finish_reason=content_filter` 终止
- **THEN** `call_skill` 结果 SHALL `is_error=true`（区别于「无显式错误的空」）

