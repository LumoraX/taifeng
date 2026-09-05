# ADR 0033：Responses 流内失败事件归一为 typed LLMError（Amends #0026）

- 状态：Accepted
- 日期：2026-09-05
- 相关：[ADR 0030](0030-codex-sse-noise-tolerance.md)、[ADR 0032](0032-usage-metadata-tolerance.md)
  是「未知形状不得升格为不可恢复故障」的另外两半；契约见
  [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md) §5.2。

## 背景

codex 与 OpenAI Responses 两条 provider 用**完全一样的一行**处理流内失败：

```python
if kind in {"response.failed", "response.incomplete", "error"}:
    raise InvalidResponseError(f"... terminal failure: {kind}")
```

两个后果：

**一、诊断信息全丢。** 2026-09-05 的真实台账连续两次跑红，报错全文就是
`Codex terminal failure: error` ——中转站到底说的是限流、鉴权还是上游 5xx，无从判断，只能靠
「重跑一次看看还红不红」来区分瞬时与确定性。第一次红三个场景、第二次红另一个场景，排查完全
依赖重试。

**二、更严重的是错误分类。** `InvalidResponseError` 继承 `InvalidRequestError`：
`retryable=False`、`failure_class=invalid_request`、recovery 配方是「调整输入 + 升级人工」。
于是一次**瞬时**的上游 5xx 被贴上「确定性客户端请求非法」的标签，`ConservativeFailurePolicy`
据此判 TERMINAL——**既不重试也不挂起**，一个本可恢复的 turn 被直接判死。

而这三种事件在 openai-openapi 里都是**有结构、有语义**的：

| 事件 | 官方字段 |
| --- | --- |
| `error`（`ResponseErrorEvent`） | `code: str\|null`、`message: str`、`param: str\|null`、`sequence_number: int` |
| `response.failed` | `response.error` = `{code, message}`（非 null 时两者必填） |
| `response.incomplete` | `response.incomplete_details.reason`，**闭集** `content_filter` \| `max_output_tokens` |

信息本来就在，是我们主动扔掉的。

## 决策

新增共享归一器 `classify_responses_stream_failure(event) -> LLMError`（`providers/_shared.py`），
**codex 与 OpenAI Responses 两条 provider 共用**，按官方字段归类到既有 taxonomy：

- **`response.incomplete`** 走闭集：`content_filter` → `ContentFilterError`；
  `max_output_tokens` → `ContextOverflowError`（输出被上限截断既非畸形也非瞬时，归 context_window
  桶——其恢复配方「压缩后重试一次」正对症，且 turn 侧有界自愈只跑一次）；
  **集合外的值才是协议违规** → `InvalidResponseError`。
- **`error` / `response.failed`** 按 `code`（子串匹配，大小写不敏感）归类：限流 / 超时 / 鉴权 /
  内容拦截 / 上下文超长 / 5xx 各归其位；`code` 认不出时回落到**正文关键字**，与
  `classify_http_error` **共用同一张关键字表**。
- **默认取值有据可依**：官方对流内 error 事件的描述是「This can happen due to an internal server
  error or a timeout」——即默认属 provider 侧瞬时故障。故认不出 code 的 `error` / `response.failed`
  归 `ServerError`（`retryable=True`、`failure_class=provider_internal`），而不是确定性客户端错误。
- **原文一并带出**：异常文本形如 `error | code=rate_limit_exceeded | param=input | Rate limit reached`，
  三个官方字段一个不丢。限流时顺带复用 `_parse_retry_after` 取服务端 hint。

`code` 不做闭集枚举：官方 `ResponseErrorCode` 持续演进，中转网关还会自造码，硬编码必然漏——
与 ADR 0030 否决「补全事件全集」同理。

## 后果

### 正向

- 瞬时故障不再被判死：限流 / 5xx / 超时现在 `retryable=True`，上层 policy 能挂起等裁决或重试。
- 排查不再靠重跑：异常文本直接说明 provider 报了什么。
- 两条 provider 同源，不会一边修好一边继续吞。
- `content_filter` 类拦截终于走到 `ContentFilterError`，与 refusal 路径的处置一致。

### 代价（**行为变化，需知悉**）

- **同样一条流内 `error`，处置从 TERMINAL 变成 SUSPEND**（在默认 `ConservativeFailurePolicy` 下，
  可重试失败落挂起等 Resume）。有人值守 / 有自动决策器的部署是改善；**无人值守的批处理会从
  「快速失败」变成「挂起等待」**，需要业务侧配 `failure_suspend_*` 旋钮或改用其他 policy。
- 归一器与 `classify_http_error` 共用关键字表，改表会同时影响两条路径。

## 被否决方案

1. **只把 code/message 拼进异常消息、分类不动**：诊断改善了，但错误分类照旧撒谎——瞬时故障仍被
   判死。用户明确指出「不应该单纯合并」，否决。
2. **给流内失败新开一个 `LLMError` 子类**：上层 policy / recovery 配方要为它专门加分支，而它的真实
   语义本来就分散在既有各类里，否决。
3. **硬编码官方 `ResponseErrorCode` 闭集**：开放演进 + 网关自造码，必然漏，否决。
4. **认不出的 code 保持不可重试**：与官方对该事件的描述相悖（「internal server error or a timeout」），
   且正是本次台账连续跑红的根因，否决。

## 验证

- `tests/llm/test_responses_stream_failure.py`：26 个用例——10 种官方 code 的归类与 retryable、
  timeout / 未知 code / 缺失 code 的默认、三个官方字段在异常文本里不丢、retry hint 解析、正文关键字
  回落、`response.failed` 嵌套 error 与 null error、`incomplete` 闭集三分支、以及经 codex 累加器
  端到端抛出的就是归一后的类型。
- `tests/llm/test_codex_sse_noise.py` 的「显式失败终态不得被当噪声吞掉」随契约更新为断言 `LLMError`
  且噪声账为空（具体分类归上面那个文件覆盖）。
- 真实回归：`docs/real-llm-ledger.md`。
