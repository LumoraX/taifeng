# ADR 0046：原生 provider 的 thinking 块与签名回传

- 状态：Accepted
- 日期：2026-09-28
- 关联：[llm-client 活文档 § reasoning 回传](../architecture/llm-client.md)；[capabilities/llm-provider-native.md § thinking-passback](../architecture/capabilities/llm-provider-native.md)

## 背景

2026-09-28 内核 review 核实：`anthropic_provider.py` 文件头宣称支持 extended thinking，流解析却只处理
`text_delta` / `input_json_delta`，请求侧也不能开启 thinking；`gemini_provider.py` 不处理 `thoughtSignature`
与 thought part。两家的 thinking 模型在工具续传时都要求**原样回传带签名的思考内容**（Anthropic：上一条
assistant 消息以 thinking 块开头；Gemini：functionCall part 带 thoughtSignature），否则 400。既有的
reasoning 回传（ADR / reasoning-content-passback）只有 `ApiMessage.reasoning: str` 文本通道，装不下签名。

## 决策

1. **不透明状态通道**：新增 `reasoning_state` 流事件与 `ApiMessage.reasoning_state`、reasoning item
   `payload.provider_reasoning`，以 provider 名为顶层键。内核只搬运不解析（R1）；只有产出它的 provider
   读自己那个键。选择不透明 dict 而非给每家建 typed 字段，是为了不把 provider 形状写进内核类型。
2. **Gemini 复用 `extra_content`**：签名是 per-functionCall 的，已有的 `extra_content.google.thought_signature`
   通道（OpenAI 兼容端点在用）正好承载，无需新通道。
3. **redacted-only 也落史**：只有 `redacted_thinking`（无可读文本）时仍建 reasoning item，否则签名丢失。
4. **冲突显式报错**：Anthropic 开 thinking 时拒绝自定义 temperature、要求 `max_tokens > budget`；显式冲突
   `InvalidRequestError`，不静默丢弃或改值。未指定 `max_tokens` 时自动留出正文额度。
5. **序列化兼容**：`ApiMessage.reasoning_state` 为 None 时 `exclude_if` 排除，旧请求的 dump 与审计
   attempt digest（跨实现固定的契约向量）逐字不变。

## 验证边界

sim / MockTransport 覆盖解析、落史、回传与端到端两轮续传。当前环境没有 Anthropic / Gemini 真实 key，
真实端点验证未执行（能力矩阵标 🧪）；接入方首次启用时应以真实 key 跑一次工具续传。
