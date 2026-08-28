# ADR 0026：Codex 代理作为独立 Provider

- 状态：Accepted
- 日期：2026-08-28

> 当前 wire 与验收细则见
> [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md)。

## 背景

部分 Codex 代理暴露 `/responses` endpoint 和 OpenAI 风格 typed items，但其契约与官方 OpenAI Responses 不完全相同：system guidance 只接受顶层 `instructions`，完整输出位于 `response.output_item.done`，而 `response.completed.response.output` 可以为空。2026-08-28 的脱敏 `gpt-5.6-luna` 探针已复现这些差异，证据见本文附录；它是 dialect 发现证据，不等同于功能验收台账。

若按模型名或代理域名在 `OpenAIResponsesClient` 内加入隐式兼容分支，会让同一个 provider identity 对应两套终态事实源，并可能把 OpenAI reasoning state 错误重放给 Codex 代理。

## 决策

新增独立 `codex` provider 与 `CodexResponsesClient`：

- 外部 capability identity 为 `provider=codex, protocol=responses`。
- wire dialect 的稳定审计名为 `codex-responses-v1`。
- bootstrap 通过 `LLM_BOOTSTRAP_PROVIDER=codex` 显式选择。
- Codex 只支持 Responses 风格 endpoint，不提供 Chat fallback。
- system prompt 使用顶层 `instructions`，`input` 始终为 typed item list。
- done items 是输出事实源，completed 是唯一完成门和 usage/response ID 来源。
- Codex 与 OpenAI provider state 双向隔离。

实现可以复用 provider-neutral Responses 协议组件，但不得靠模型名或 base URL 自动切换 provider identity。

## 后果

### 正向

- OpenAI 官方客户端保持稳定，代理差异不污染其协议和验收结论。
- 配置、日志、台账和 durable state 能明确区分 OpenAI 与 Codex。
- Codex 代理协议变化可在独立 client 中演进。

### 代价

- 增加一个公共 provider、bootstrap 分支和真实回归矩阵。
- Responses 的共享解析组件需要显式参数化 provider identity 与终态来源，测试量增加。
- 切换 OpenAI/Codex 时旧 reasoning state 会被拒绝，业务需新建 thread 或先清理不兼容历史。

## 被否决方案

1. 在 `OpenAIResponsesClient` 中按模型名或域名自动兼容：身份不透明，容易发生状态串用，否决。
2. 用环境变量为 OpenAI client 打开 dialect flag：配置组合继续共享 OpenAI identity，仍不能保证 durable state 隔离，否决。
3. 让 Codex 回退到 Chat：真实代理的 Chat 路径连纯文本都未满足协议，且会扩大不可靠表面，否决。

## 脱敏探针证据（2026-08-28）

探针使用用户明确配置的第三方 Codex 代理与 `gpt-5.6-luna`；API key、真实 hostname、请求正文和
`encrypted_content` 均未写入仓库。以下只保留决定 dialect 所需的最小结构事实。

### Case A：system item 被拒绝

```json
{
  "request": {
    "method": "POST",
    "url": "https://<redacted-host>/<api-root>/responses",
    "body_shape": {"model": "gpt-5.6-luna", "input": ["role=system", "role=user"], "stream": true}
  },
  "result": {
    "accepted": false,
    "safe_error": "System messages are not allowed"
  }
}
```

### Case B：顶层 instructions + typed input 被接受

```json
{
  "request": {
    "method": "POST",
    "url": "https://<redacted-host>/<api-root>/responses",
    "body_shape": {
      "model": "gpt-5.6-luna",
      "instructions": "<redacted-text>",
      "input": ["typed-user-message"],
      "stream": true
    }
  },
  "result": {
    "http_status": 200,
    "sse_types": [
      "response.created",
      "response.in_progress",
      "response.output_item.added",
      "response.content_part.added",
      "response.output_text.delta",
      "response.output_text.done",
      "response.content_part.done",
      "response.output_item.done",
      "response.completed"
    ],
    "output_fact_location": "response.output_item.done.item",
    "completed_output": [],
    "completed_usage_present": true
  }
}
```

探针未覆盖图片、tool、reasoning 热/冷恢复、取消、崩溃恢复或未授权 sink 脱敏，因此不得据此把 Codex
provider 标为真实矩阵 PASS。
