# Codex Responses Provider 能力契约

> Provider 决策见 [ADR 0026](../../decisions/0026-independent-codex-provider.md)，敏感 request 审计修订见
> [ADR 0027](../../decisions/0027-sensitive-llm-request-audit.md)。本契约的 wire dialect 稳定名为
> `codex-responses-v1`。

## 1. 范围与身份

本能力为 Codex 代理提供独立的 Responses 风格 provider，不属于 OpenAI provider 的兼容分支。

- 公共客户端名为 `CodexResponsesClient`，稳定导入路径为 `taifeng.CodexResponsesClient` 与
  `taifeng.llm.providers.codex.CodexResponsesClient`。
- `ModelCapabilities.provider` 必须为 `codex`，`protocol` 必须为 `responses`，并声明文字、图片与
  provider state 输入能力。
- Codex provider 只实现 `/responses`，不得实现或回退到 `/chat/completions`。
- `OpenAIChatClient`、`OpenAIResponsesClient` 与 `OpenAICompatClient` 的 wire、capability 和默认行为
  不得改变。客户端不得按模型名、base URL 域名或响应形状隐式切换 dialect。

## 2. Bootstrap 配置契约

共享 example bootstrap 使用统一环境变量：

```dotenv
LLM_BOOTSTRAP_PROVIDER=codex
LLM_BOOTSTRAP_PROTOCOL=responses
LLM_BOOTSTRAP_API_KEY=...
LLM_BOOTSTRAP_MODEL=gpt-5.6-luna
LLM_BOOTSTRAP_BASE_URL=https://your-codex-proxy.example/v1
```

### 2.1 真值表

| provider | protocol | 结果 |
| --- | --- | --- |
| `codex` | 缺失/空字符串 | 接受并规范化为 `responses` |
| `codex` | `responses` | 接受 |
| `codex` | `chat`、`response` 或其他值 | 在构造 client 前拒绝 |
| `openai` | `chat` / `responses` | 保持既有选择逻辑 |

- `LLM_BOOTSTRAP_PROVIDER=codex` 必须构造 `CodexResponsesClient`。
- Codex 缺省模型为 `gpt-5.6-luna`，业务可通过 `LLM_BOOTSTRAP_MODEL` 显式覆盖。
- Codex 只读取统一 `LLM_BOOTSTRAP_API_KEY`、`LLM_BOOTSTRAP_MODEL` 与
  `LLM_BOOTSTRAP_BASE_URL`；不得读取旧 `LLM_BOOTSTRAP_OPENAI_*` 字段。
- Codex base URL 必填。它必须是带 hostname 的绝对 `http` 或 `https` URL，不得含 userinfo、query 或
  fragment；尾部 `/` 在构造时去除。路径必须指向 API root（例如 `/v1`），若去除尾部 `/` 后以
  `/responses` 结尾则拒绝，防止生成重复 endpoint。
- endpoint 只能由规范化后的 `<base_url>/responses` 得到；内核和 bootstrap 不得硬编码代理域名。
- `require_api_key=True` 且统一 key 缺失/空时，bootstrap 必须拒绝；`require_api_key=False` 时仍执行
  protocol/base URL 校验并用空 key 构造 client，metadata 不得产生 `api_key_tail`。
- bootstrap metadata 必须包含
  `provider="codex"`、`protocol="responses"`、`dialect="codex-responses-v1"`、`model` 与规范化的
  `base_url`；只有非空 key 才允许加入掩码后的 `api_key_tail`。

## 3. 请求 wire

### 3.1 Instructions 与有序 input

- `input` 必须始终是有序 JSON list；即使只有一条纯文本输入也不得退化为字符串。
- 对 `ApiRequest.system_prompt` 仅过滤长度为零的元素；每个保留元素不得 trim、规范化换行或修改
  Unicode，其原始字符串内容必须逐字节保留，再用两个 ASCII LF（`"\n\n"`）连接为顶层
  `instructions`。
- 若过滤后无元素，请求必须省略 `instructions`；不得发送空字符串，也不得在 `input` 中生成 synthetic
  `role=system` message。
- history 中 budget hint、memory page-in、compaction summary 等运行时 `role=system` text item 必须按
  canonical 遍历顺序从 `input` 移除，并追加到静态 system prompt 之后的顶层 `instructions`；非文本
  system content 网络前拒绝。该折叠保持 instruction authority，同时兼容不接受 input system item 的
  `codex-responses-v1` 代理。
- user/assistant message、function call、function output 与 reasoning state 必须按 canonical 顺序作为
  typed items 放入 `input`。

Canonical 示例：

```json
{
  "model": "gpt-5.6-luna",
  "instructions": "第一段原文\n\n第二段原文",
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {"type": "input_text", "text": "描述图片"},
        {"type": "input_image", "image_url": "data:image/png;base64,<redacted>", "detail": "high"}
      ]
    }
  ],
  "store": false,
  "stream": true,
  "include": ["reasoning.encrypted_content"]
}
```

图片必须使用 user message content 中的 `input_image` Data URL；canonical base64 只在网络边界临时
投影。assistant/system 图片必须在网络前拒绝。请求固定 `store=false`、`stream=true`、
`include=["reasoning.encrypted_content"]`，不得发送 `previous_response_id`。

Codex 图片输入规范性继承 [LLM 图片输入契约](llm-image-input.md)：provider 声明 image capability 不会自动
启用业务图片输入；调用方仍须显式注入默认关闭的 `ImageInputPolicy`，并在 durable acceptance 前完成 canonical
base64、MIME/文件签名、宽高/单帧、单项/总尺寸、SHA-256 与图片数量门禁。GPT-5.6 输入 token 估算和最终请求
字节门禁必须复用该契约的同一 estimator/policy，不得在 Codex client 另设宽松路径。

### 3.2 Tool、function output 与 structured output

- 每个 tool 必须使用扁平形状
  `{"type":"function","name":...,"description":...,"parameters":...}`，不得嵌套 `function`，也不得
  发送 tool-level `strict`。
- 只要 tools 非空，就必须发送 `parallel_tool_calls=<ApiRequest.parallel_tool_calls>`；tools 为空时省略
  `tools` 与 `parallel_tool_calls`。
- 历史 function call 映射为
  `{"type":"function_call","call_id":...,"name":...,"arguments":...}`；对应结果映射为
  `{"type":"function_call_output","call_id":...,"output":...}`，且必须保持原始配对顺序。
- `response_format` 非空时必须精确映射为
  `{"text":{"format":{"type":"json_schema","name":...,"schema":...,"strict":...}}}`；为空时省略
  `text`。
- `reasoning_effort`、`temperature`、`max_output_tokens` 与最终 UTF-8 JSON 字节门禁沿用
  `ApiRequest`/Responses 既有语义。

## 4. Provider state 隔离

Codex reasoning state 的 envelope identity 必须精确等于：

```json
{"provider":"codex","protocol":"responses","item_type":"reasoning"}
```

payload 规则：

- 只允许 `id`、`type`、`encrypted_content`、`summary`、`status` 五个键；出现其他键即拒绝。
- `type` 必须精确为 `reasoning`；`id` 与 `encrypted_content` 必须为非空字符串。
- `summary` 若存在必须为 list；`status` 若存在必须为字符串。
- `CodexResponsesClient` 只接受上述 Codex state；OpenAI 或其他 state 必须在网络前以
  `InvalidHistoryError` 拒绝。
- `OpenAIResponsesClient` 以及所有其他 provider/client 也必须在网络前做 exact envelope identity match，
  不得接受、静默丢弃或重写 Codex state。
- Codex state 随 `llm_sample_id` 原子提交、按 sample group 压缩，不使用 `previous_response_id`。

## 5. SSE 状态机与终态

### 5.1 事件配对与索引

- 只允许 terminal output item 类型 `reasoning`、`message`、`function_call`；hosted tool、computer use、
  image generation 或其他 item 类型全部 fail closed。
- message content part 只允许 `output_text` 与 `refusal`。`refusal` delta/part 必须遵守同样的索引、配对和
  逐字节一致性校验；refusal 不得与 `output_text` 混合。任一非空 refusal 以 `ContentFilterError` 结束
  attempt，不进入 durable conversation、不发布 normalized/completed；空 refusal part 是 invalid response，
  同样不得提交。
- 输出索引必须从 `0` 开始连续递增；不得重复、跳号、倒序或使用 bool/非整数。
- 每个索引必须恰好经历一次 `response.output_item.added`，随后是零或多个与 item 类型匹配的 delta，
  最后恰好一次 `response.output_item.done`。delta/done 在 added 前到达、done 缺失或重复均须拒绝。
- `added.item.type` 和非空 `item.id` 必须与同索引 done item 完全一致；function call 的非空
  `call_id`/`name` 也必须一致。done 后不得再出现该索引的 delta、content-part 或第二个 done。
- message content-part 若出现，`content_index` 同样须从 `0` 连续，added/delta/done 成对；terminal part 的
  type 与文本必须和对应 delta 逐字节一致。
- text、reasoning summary 与 function arguments 只要出现过 delta，其拼接结果必须和 done item 对应字段
  逐字节一致；不得用 done 覆盖冲突 preview。
- 至少必须有一个合法 done item；零 done item 即使 completed.output 为空也视为无输出事实并拒绝。

### 5.2 completed 完成门

- `response.completed` 是唯一成功完成门，必须恰好出现一次；其 `response` 必须是 object，`id` 必须为
  非空字符串，`status` 必须精确为 `completed`。
- `usage` 必须是 object，`input_tokens`、`output_tokens`、`total_tokens` 必须是非 bool 的非负整数，且
  `total_tokens == input_tokens + output_tokens`；存在的 token detail 字段必须是 object，所含计数也必须为
  非 bool 的非负整数。
- done items 是 Codex 输出事实源。若 `completed.response.output` 是空 list，则仅使用已验证 done items；若
  是非空 list，则数组 position 就是隐式 `output_index`，显式 `output_index` 若存在必须等于 position；该
  数组必须与 done items 在索引、顺序、类型、身份和所有白名单正文/状态字段上 canonical 等价，否则 fail
  closed。非 list 的 output 一律拒绝。
- `response.failed`、`response.incomplete` 与 `error` 是失败终态。提前 EOF、重复 completed、completed 前
  缺少 done、或 completed 后 EOF 前出现任何新的非空 SSE data event 均须失败。
- 客户端只在 completed 校验成功且确认其后无新 event 时，按顺序发布唯一 `normalized_output`，随后发布
  唯一 `completed`。失败、取消或未知终态不得发布 partial `normalized_output`/`completed`。

## 6. Durable、脱敏与授权 sink

“不泄漏”定义为：图片正文和 `encrypted_content` 不得离开为会话恢复明确授权的 durable store。

| sink | 图片正文 | `encrypted_content` | 要求 |
| --- | --- | --- | --- |
| canonical `MessageStore` / `SessionJournal` conversation item、最终 response checkpoint | 可保存 | 可保存 | 恢复事实源；完整性、访问控制、加密与保留期由部署方治理 |
| strict `llm_request_committed` intent | 仅 descriptor + 全量 request digest | 删除键和值，仅保留全量 request digest | observer 只能收到结构化安全投影 |
| strict attempt checkpoint / logical response | 不应含 request 图片 | 可保存 | 仅保存 verified normalized provider state 以支持恢复 |
| `LlmRequestRecorded`、普通 request capture | 仅 media type/size/SHA-256/detail descriptor | 删除键和值 | 禁止 Data URL/base64/ciphertext |
| telemetry、OTel、日志、错误、debug repr | 仅 descriptor/digest | 禁止 | 不得输出正文、Data URL、密文或持久 secret |

`llm_request_committed` 必须按
[SessionJournal business integration](session-journal-business-integration.md) §8 的
`LlmRequestCommittedV2` 精确字段，同时保存安全投影、redaction manifest 与脱敏前 canonical attempt 的
SHA-256 digest；digest 在内存中计算，不得先把原文写入临时文件或旁路 sink。它按 ADR 0027 修订 ADR 0025
对敏感 request 的要求：canonical conversation/provider state 仍是恢复事实源，而 request intent 只证明已
提交 dispatch 意图；关联 checkpoint 只证明 attempt 已进入受审计 client 执行阶段并形成 durable 终态，
不证明请求字节实际离开进程。

任何新增 sink 默认属于未授权 sink，除非契约明确把它列为 canonical recovery store。脱敏必须在事件/observer
对象构造前完成，不能依赖下游消费者自行清洗。

## 7. 取消、崩溃恢复与 strict audit

- stalled HTTP read 必须与 `CancellationToken` 竞争；token 触发后取消 read、退出 stream scope 并关闭连接，
  不得等待 provider timeout。
- 取消可保留已发布的非 durable preview delta，但不得发布 normalized/completed，也不得提交部分 sample；
  strict audit 模式由于 checkpoint-before-delta，会在 cancelled checkpoint definite ack 后传播取消且不发布
  缓冲 preview。
- done items 到达但 completed 未到达即崩溃/EOF：结果为 unknown/incomplete，不提交 sample，不执行工具；
  strict audit 在 request 已 dispatch 后记 UNKNOWN、freeze，禁止自动重试。
- completed 已验证但 response checkpoint 尚未 definite ack 即崩溃：结果仍为 UNKNOWN，freeze，禁止自动重试。
- 所有 Codex logical sample（legacy `AtomicBatchMessageStore` 与 strict SessionJournal）统一使用
  `(thread_id, sample_scope_id, turn_index, iteration)` 确定性生成 `llm_sample_id`；通常
  `sample_scope_id=submission_id`。detached child 的 Resume/Rewind 为保持 UI 分轨会继续用 child thread id
  作为事件 `submission_id`，但必须用本次 Resume/Rewind submission id 作为 sample scope，使新采样不复用
  已提交原子批次。strict conversation items 在 `llm_response_committed` 同一原子 batch 中携带该 ID，它与
  Journal 的 operation/attempt ID 正交。相同 logical sample 重放不得生成新 identity。
- 当前 strict SessionJournal 不支持打开已有 Journal、resume 或跨进程 recovery。response checkpoint definite
  ack 后、`llm_response_committed` 原子 batch ack 前崩溃，或 sample ack 后、function call intent/output 收敛
  前崩溃，都必须进入 `UNKNOWN/freeze/RECOVERY_REQUIRED`，不得自动请求 provider、自动派发 tool 或声称已经
  恢复。checkpoint/record 中的稳定 operation、attempt、submission、turn、iteration identity 仅为未来显式
  recovery 阶段保留；在 SessionJournal 活契约和 capability gate 扩展前不得实现自动恢复。
- legacy store 不提供跨崩溃 exactly-once 保证；已提交 function call 与缺失 output 的冷恢复只可按现有
  MessageStore resume 契约处理，外部非幂等或结果不明不得猜测成功或自动重复副作用。
- strict audit 只接受仓库逐一 allowlist 的 exact `CodexResponsesClient` one-attempt 类型：一次 `stream` 必须
  恰好对应一次 HTTP request，client 内部不得 retry；每次 attempt 必须遵守
  request-intent definite ack → dispatch → response-checkpoint definite ack → buffered events 的顺序。

## 8. 验证边界

单元/Sim 测试必须覆盖：

- 顶层 instructions 的空值过滤、原字节保留与 canonical JSON；input 始终为 list；
- 单图/多图 wire、tools/function output/text.format、最终 UTF-8 字节上限；
- done-item accumulator、空 completed output、非空 completed output 等价/冲突、索引和 event 配对；
- completed 的 ID/status/usage、completed 后事件、失败终态、EOF、取消；
- provider-state payload 白名单和 Codex/OpenAI/其他 provider 双向隔离；
- 上述 crash-window 分类、stable sample identity、`RECOVERY_REQUIRED` 门禁、strict audit one-attempt 与所有
  未授权 sink 脱敏。

Sim/Mock 只证明协议、持久化与故障语义，不证明视觉理解。真实矩阵必须使用 `provider=codex`，覆盖纯文本
instructions、单图、多图顺序、图片工具调用、encrypted state 热重放与 JSONL 冷恢复。只有代理返回合法非零
usage、语义断言通过且敏感正文未泄漏到授权 durable store 之外时才能记为 PASS；否则台账必须记录 FAIL 或
NOT_EXECUTED。
