# ADR 0039：网络重试上事件总线——经 session 可选观察者，而非流内事件

- 状态：Accepted
- 日期：2026-09-13
- Amends：ADR 0037（`RetryingModelClient` 与 attempt 契约互斥；本 ADR 不改互斥，只补可观测）
- 关联：[llm-client 活文档](../architecture/llm-client.md)；[reactive-compaction-recovery 契约](../architecture/capabilities/reactive-compaction-recovery.md)（`ProviderRetry` 事件定义处）；openspec change `network-retry-observable`

## 背景

2026-09-13 对「中转 / 上游不稳定」这族故障做处置链路审计（分类 → 重试 → 挂起 → 无人值守兜底），
四层实测闭合，唯独**重试这一层在事件总线上是静默的**：

- `RetryingModelClient`（ADR 0037 接线）在每次退避重试时只写一条 `logger.info`，不 emit 任何 `EventMsg`。
  经真实 Engine 跑到重试耗尽的实测：网络 attempt = 3，`provider_retry` 事件 = 0，
  该 turn 的事件只有 `turn_started → rewind_checkpoint_recorded → turn_suspended`。
- CLAUDE.md 的 R3 红线点名要求关键路径打 `provider_retry`；该事件**已定义**（`loop/event.py`），
  但只服务 A1 上下文超长自愈那一条路径（`reason=context_overflow`）。
- `telemetry/otel_sink.py` 里留着自认的欠账：`taifeng.provider.retries 暂搁待 ProviderRetry 事件落地`。

代价是具体的：同期中转出现 ~17% `server_is_overloaded` 窗口，从系统自身的可观测性**回答不了**
「中转是否稳定」，只能另写裸 httpx 探针到外部量。这与 R3 的立意正相反。

## 决策

### 1. 复用 `ProviderRetry`，以 `reason` 区分来源，不新增事件 kind

网络退避重试 emit `provider_retry`，`data.reason` 取触发重试的 `LLMError.kind`
（`transient_network` / `rate_limit` / `server_error` …）；overflow 自愈保持 `reason=context_overflow`。
网络重试另带 `attempt` / `max_attempts` / `delay_seconds` / `failure_class` / `error_kind` /
`transport_phase` / `retry_after_seconds`。既有按 `reason == "context_overflow"` 分流的消费者零影响；
R3 红线里的 `provider_retry` 名字第一次名副其实。

### 2. 观察者经 session **可选协议**注入，宿主 `getattr` 探测

`_RetryingSession` 暴露 `set_retry_observer(observer)`；`TurnSample.sample_once` 创建 session 后
`getattr(sess, "set_retry_observer", None)` 探测、可调用即注入 `partial(self._emit_provider_retry, iteration)`。
非重试型 session 无此方法 → 静默跳过。探测风格与既有 `last_attempt_checkpoint` 同一套，
`ModelClient` / `ModelClientSession` 协议签名**不动**，五家 provider 与所有包装器零改动；
透明包装（台账录制 `_RecordingSession`）靠 `__getattr__` 转发即可接通（有回归用例钉死）。

`llm/` 层只产纯数据 `RetryAttempt`（frozen dataclass），不依赖 `loop/` 的事件类型；EventMsg 化在 `loop/turn_sample.py`。

### 3. 不往 `ResponseEvent` 流里塞新 kind

否决「yield 一个 `retry` 流内事件」：金样校准（llm-sim-conformance 的形状漂移红线）按事件 kind 序列签名，
重试次数是随机量，流内事件会让同一场景的形状随网络状况漂移；且每个流消费者都要学会忽略新 kind。
观察者回调路径与 `ResponseEvent` 流完全正交。

### 4. 退避**之前** emit

观察者在 `_sleep_or_cancel(delay)` 之前被 await（有回归用例钉顺序）。运维看到的是「正在退避 1.25s」，
不是事后补记；限流 hint 可达数十秒，事后补记等于这几十秒里事件流一片死寂。

### 5. OTel counter `taifeng.provider.retries` 落地

按 `reason` / `failure_class` 两维计数；console 渲染补专用 tag（`llm ↻`），
台账 R3 完整性审计（「所有发出的 kind 都有专用渲染」）覆盖到它。

## 备选方案（否决理由）

| 方案 | 否决理由 |
| --- | --- |
| 流内 `ResponseEvent(kind="retry")` | 金样形状随重试次数漂移；所有流消费者要忽略新 kind（见决策 3） |
| `ModelClient.session(..., emit=)` 加参数 | 改协议签名，五家 provider + 全部包装器同步跟改，收益只是省一次 getattr |
| 内核默认套 `RetryingModelClient` | 与 strict audit 一次 attempt 契约互斥，ADR 0037 明确留给业务侧选择；不在本 ADR 范围 |
| 事后从 session 读 `retry_log` 列表 | 事件晚于退避出现，长退避期间总线静寂（见决策 4） |

## 后果

- R3 在重试这一层闭合：每次网络重试在事件总线、console、OTel 三处都可见；业务侧零改动即得。
- 审计中同时确认但**不在本 ADR 处理**的两件事，分别另案：
  - provider 级断路器 / 健康度不存在（持续故障时每个 turn 各烧满 attempt）→ openspec change `provider-circuit-breaker`（先设计）。
  - `RetryConfig.retryable_kinds` 与 `LLMError.retryable` 是两套「可重试」真相：`UnreliableFinishError.retryable=True`
    但其 kind `unreliable_finish` 不在默认集合，装饰器不会重试它，直接落挂起。接入方需显式加入；默认集合是否该并入另议。
