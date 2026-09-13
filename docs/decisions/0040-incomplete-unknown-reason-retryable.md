# ADR 0040：`response.incomplete` 未知 reason 按可重试默认处置，不再判协议违规

- 状态：Accepted
- 日期：2026-09-13
- Supersedes：ADR 0033 中「`incomplete_details.reason` 走闭集，集合外的值才是协议违规 → `InvalidResponseError`」一款；其余条款不变
- 关联：[llm-codex-provider 契约 §5.2](../architecture/capabilities/llm-codex-provider.md)；ADR 0034（记账明细不得判死 turn，同一哲学）

## 背景

ADR 0033 把 Responses 流内三种失败归一为 typed `LLMError`，其中对 `error` / `response.failed` 的 `code`
明确**不做闭集**：「官方 `ResponseErrorCode` 持续演进，中转网关还会自造码，硬编码必然漏」，认不出的 code
默认归 `ServerError`（可重试）。但同一份 ADR 对 `response.incomplete` 的 `incomplete_details.reason`
却采用了相反标准：`content_filter` / `max_output_tokens` 之外**一律判协议违规** → `InvalidResponseError`
（`retryable=False`，`failure_class=invalid_request`）。

2026-09-13 复核处置链路时实测：`reason="new_reason_2027"` 与 `incomplete_details` 缺失两种输入均落
`InvalidResponseError`，保守失败策略下 turn **直接判死**。这正是此前定下的规矩要禁止的形态——
「只对官方要求且驱动决策的字段报错，未知的一律放行，否则上游新特性第一天上线内核就先死给你看」
（同一规矩已在 ADR 0034 用于记账明细）。`reason` 与 `code` 同样是会演进的官方枚举，没有理由一个开放一个封闭。

## 决策

集合外或缺失的 `incomplete_details.reason` → `ServerError`（`retryable=True`，`failure_class=provider_internal`），
与认不出的 error code **同一默认**：外层 `RetryingModelClient` 退避重试，仍败则按失败策略挂起等人裁决；
异常消息保留原文 `reason` 供排查。已知值处置不变：`content_filter` → `ContentFilterError`（终态），
`max_output_tokens` → `ContextOverflowError`（走压缩后重采样的有界自愈）。

## 备选方案（否决理由）

| 方案 | 否决理由 |
| --- | --- |
| 维持 `InvalidResponseError` | 把「我不认识」等同于「你违规」，上游演进即事故；与 code 的处置标准自相矛盾 |
| `UnreliableFinishError`（不在默认 `retryable_kinds`，直接挂起等人） | 语义贴合但绕过自动重试；未知 reason 最常见成因仍是上游瞬时状况，先自动退避再挂起更省人力，且与 code 默认一致 |
| 扩展闭集把已知新值逐个加进来 | 追不上演进，且不解决「第一次出现」那天的行为 |

## 后果

- 上游新增 incomplete 原因时，turn 走「退避重试 → 挂起」而非判死；诊断文本不丢。
- `tests/llm/test_responses_stream_failure.py` 的参数化用例由「协议违规」翻转为「可重试默认」，
  失败类断言从 `InvalidResponseError` 改为 `ServerError`；这是有意的契约变更，不是回归。
- 触及 `src/taifeng/llm/`，按红线全量重跑真实台账。
