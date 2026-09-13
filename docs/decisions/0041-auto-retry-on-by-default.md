# ADR 0041：内核默认套有界重试；`unreliable_finish` 并入默认可重试集合

- 状态：Accepted
- 日期：2026-09-13
- Supersedes：ADR 0037 Non-goal「把 `RetryingModelClient` 做成默认包装进 `EnginePool`——业务侧显式选择」；ADR 0039 后果节「`retryable_kinds` 与 `LLMError.retryable` 两套真相另议」
- 关联：[llm-client 活文档](../architecture/llm-client.md)；[configurable-knobs §7](../configurable-knobs.md)；openspec change `auto-retry-default-on`

## 背景

ADR 0037 把 `retry_async` 真正接线成 `RetryingModelClient`，但**留给业务侧显式套**：理由是它与 strict audit
的「一次 `stream` 恰一个 attempt」契约互斥。2026-09-13 处置链路审计实测的后果：接入方 qiuben api 的
`build_model_client()` 返回裸 `OpenAICompatClient`（模块 docstring 还写着「taifeng 已处理 retry」），全 `api/`
搜不到 `RetryingModelClient`，MDT 也不是 Celery task——**任何一层都没有重试**。中转一次瞬时抖动（同期实测
~17% `server_is_overloaded` 窗口）直接把专科轨打成挂起、等医生点「重试」。用户裁决：**默认自动 retry 开启**。

同批还有一处「两套真相」：`UnreliableFinishError.retryable=True`，但其 kind `unreliable_finish` 不在
`RetryConfig.retryable_kinds` 默认集合，装饰器不会重试它——而这恰是 qiuben 最常见的抖动形态（网关把上游
`MALFORMED_FUNCTION_CALL` 错标成 `content_filter`，「6 次 3 中、重跑即过」）。默认开启却不覆盖它，等于没开。

## 决策

### 1. 三处默认包装，一个幂等入口

`llm/retrying.with_default_retry(client, *, config=None, enabled=True)` 是唯一入口；`AgentEngine.__init__`、
`AgentEnginePool.__init__`、`EnginePool.create`（在压缩器 / recall 构建**之前**）三处都经它。幂等靠三条规则，
重复经过无副作用：

| 输入 | 处置 | 为什么 |
| --- | --- | --- |
| `enabled=False`（`auto_retry=False`） | 原样 | 业务自管重试，或测试复现「重试已耗尽」 |
| 已套过（`getattr(client, "bounded_retry", None)` 非 None） | 原样 | 防双层包装把 attempt 上限乘起来（3×3=9）；标记是 `RetryingModelClient` 的属性，台账录制 `RecordingClient` 这类 `__getattr__` 透明包装可穿透 |
| `AttemptObservableModelClient`（strict audit 适配器） | 原样 | 一次 `stream` 恰一个 attempt 是它的契约（ADR 0037），套重试破坏 checkpoint lineage——audit 优先，互斥关系不变 |

池级也包装，是为了让压缩摘要（`HandoffCompactionStrategy`）与 skill recall / verifier 这些池内 LLM 侧调用同享默认重试；
引擎级再包装是为裸 `AgentEngine` 用法兜底。`auto_retry` / `retry_config` 两个旋钮从 `EnginePool.create` 一路透传到
每个引擎（`pool_session.py` 装配点），池级关闭时引擎不得再自行包装。

### 2. 默认策略就是生产策略

`retry_config=None` → `RetryConfig()`：3 次 attempt、500ms 起指数退避封顶 30s、尊重服务端 `retry_after`、
只在零产出时重发（ADR 0037 正确性基石不变）。**不**为测试给快配置——测试要么注入 `retry_config`，要么
`auto_retry=False`。

### 3. `unreliable_finish` 并入默认集合

它只在接入方声明 `trust_finish_reason=False` 时抛出，且定义上零产出，重发无重复投递风险；并入后默认集合为
`{rate_limit, transient_network, server_error, unreliable_finish}`。`retryable_kinds` 仍是显式白名单
（不改成读 `LLMError.retryable`——白名单让「哪些 kind 会自动重发」在配置里一眼可见）。

## 波及面（实测）

全量 2461 用例，恰 3 条红，全是「剧本只抛一次故障、断言随后挂起 / 续跑」的用例（`test_system_retry_suspends_turn`
/ `test_resume_system_retry` / `test_kernel_system_retry_expire_auto_retries`）：默认重试吞掉了它们的一次性故障
（一条因尊重 3s hint 慢了 6s 后剧本耗尽转 `turn_failed`）。三者前提本就是「重试已耗尽」，各加 `auto_retry=False`
一行；其余 2458 条行为与耗时不变（`--durations` 无新增慢用例）。

## 后果

- 接入方零改动即得有界重试；qiuben 的 `build_model_client()` 不再需要自己套（钉到含本 ADR 的发版即可）。
- examples 的 `_provider_bootstrap.build_model_client(retry=True)` 显式套保留（幂等，且供不经引擎的直连脚本）。
- 与 strict audit 的互斥由 `with_default_retry` 自动处理：传入 audit 适配器即不套，无需接入方记住 `retry=False`。
- 每次重试仍经 ADR 0039 的 `provider_retry` 可观测。
