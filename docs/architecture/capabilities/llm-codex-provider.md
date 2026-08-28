# Codex Responses Provider 能力契约

## 范围与身份

本能力为 Codex 代理提供独立的 Responses 风格 provider，不属于 OpenAI provider 的兼容分支。

- 公共客户端名为 `CodexResponsesClient`，稳定导入路径为 `taifeng.CodexResponsesClient` 与 `taifeng.llm.providers.codex.CodexResponsesClient`。
- `ModelCapabilities.provider` 必须为 `codex`，`protocol` 必须为 `responses`，并声明文字、图片与 provider state 输入能力。
- Codex provider 只实现 `/responses`，不实现或回退到 `/chat/completions`。
- OpenAIChatClient、OpenAIResponsesClient 与 OpenAICompatClient 的现有 wire、capability 和默认行为不得改变。

## 配置契约

共享 example bootstrap 使用统一环境变量：

```dotenv
LLM_BOOTSTRAP_PROVIDER=codex
LLM_BOOTSTRAP_PROTOCOL=responses
LLM_BOOTSTRAP_API_KEY=...
LLM_BOOTSTRAP_MODEL=gpt-5.6-luna
LLM_BOOTSTRAP_BASE_URL=https://your-codex-proxy.example/v1
```

- `LLM_BOOTSTRAP_PROVIDER=codex` 必须构造 `CodexResponsesClient`。
- Codex protocol 缺省为 `responses`；若显式配置，只允许 `responses`。
- Codex base URL 必须显式提供，内核和 bootstrap 不得硬编码任何第三方代理域名。
- Codex 缺省模型为 `gpt-5.6-luna`，业务可显式覆盖。
- API key、model 与 base URL 继续使用统一 `LLM_BOOTSTRAP_*` 字段，不增加代理厂商专属 secret 名称。

## 请求 wire

- endpoint 必须为 `<base_url>/responses`。
- `input` 必须始终是有序 list，不得退化为字符串。
- system prompt 必须按原顺序合并为顶层 `instructions` 字符串；不得生成 `role=system` input item。
- user/assistant message、function call、function output 与 reasoning state 必须继续作为有序 typed items 放入 `input`。
- 图片必须使用 user message content 中的 `input_image` Data URL；canonical base64 只在网络边界临时投影。
- 请求固定 `store=false`、`stream=true`、`include=["reasoning.encrypted_content"]`，不得使用 `previous_response_id`。
- tools、structured output、reasoning effort、temperature、max output tokens 与请求字节门禁沿用 Responses 语义。

## 流式终态

- `response.output_item.done` 是 Codex provider 的 durable output item 事实源；客户端必须按 `output_index` 暂存 message、reasoning 与 function call。
- `response.completed` 是唯一完成门，只提供 response ID、usage 与完成状态；其 `response.output` 允许为空。
- 只有在恰好一个 `response.completed` 到达后，客户端才能从已收齐的 done items 发布唯一 `normalized_output`，且必须先于 `completed`。
- 已观察的 text/reasoning/function argument delta 必须与对应 done item 逐字节一致。
- done item 缺失、索引重复、索引乱序、身份为空、delta 不一致、重复 completed 或流提前 EOF 都必须 fail closed，不得提交部分输出。
- `response.failed`、`response.incomplete` 与 `error` 必须作为失败终态处理，不得伪造 completed。

## Provider state 隔离

- Codex reasoning envelope 的 provider 必须为 `codex`、protocol 为 `responses`、item_type 为 `reasoning`。
- Codex provider 只接受 `provider=codex` 的 reasoning state；OpenAI 或其他 provider state 必须在网络前以 `InvalidHistoryError` 拒绝。
- OpenAIResponsesClient 同样不得接受 Codex state。
- Codex state 随 `llm_sample_id` 原子提交、按 sample group 压缩，并遵循现有 unknown-outcome 冷恢复规则。
- 图片正文与 `encrypted_content` 不得进入日志、telemetry、普通 request capture 或 strict attempt observer。

## 实现边界

- Codex 必须是独立公共 client 和 bootstrap provider；不得通过模型名、base URL 域名或隐藏分支改变 OpenAI client 行为。
- 可复用 Responses 的纯协议解析、错误分类、usage、SSE 取消与敏感内容脱敏组件，但 provider identity、请求构造和终态策略必须由 Codex client 显式选择。
- 长时读取必须由 `CancellationToken` 抢占；网络调用不得阻塞 actor。

## 验证边界

- 单元测试必须覆盖顶层 instructions、有序 input list、图片 wire、done-item 终态、空 completed output、usage、工具调用、provider-state 双向隔离、冲突/缺失终态、取消与字节上限。
- Sim/Mock 测试只证明协议与持久化，不证明视觉理解。
- 真实矩阵必须使用 `provider=codex`，覆盖纯文本 system instructions、单图、多图顺序、图片工具调用、encrypted state 热重放与 JSONL 冷恢复。
- 真实矩阵只有在代理返回非零 usage、语义断言通过且没有敏感正文落盘时才能记为 PASS；否则台账必须如实记录 FAIL 或 NOT_EXECUTED。
