# ADR 0042：provider 级断路器——跨 turn 的上游健康度，open 态快速失败

- 状态：Accepted
- 日期：2026-09-17
- 关联：[llm-client 活文档](../architecture/llm-client.md)；[capabilities/provider-circuit-breaker.md](../architecture/capabilities/provider-circuit-breaker.md)；[configurable-knobs §7.6](../configurable-knobs.md)；ADR 0037 / 0039 / 0041（重试三部曲）；openspec change `provider-circuit-breaker`

## 背景

2026-09-13 处置链路审计：分类 → 重试 → 挂起 → TTL 兜底四层闭合，但**跨 turn 的 provider 失败记忆为零**
（`denial_breaker.py` 只管 turn 内权限拒绝；`llm/` `loop/` 内 `consecutive` / `failure_count` 零匹配）。
后果是持续性故障下的纯浪费：中转宕 10 分钟，每个 turn 各自烧满 `RetryConfig.max_attempts`（默认 3）+
退避（最长 30s/次）再挂起，N 路并发 = 3N 次注定失败的请求。既没有快速失败，业务侧也拿不到「上游已降级」
的信号去做降级 / 排队 / 告警——同期中转实测 ~17% `server_is_overloaded` 窗口正是这种形态。

按 ADR 0017 立项四规则，这是**内核机制缺口**（规则 ①）：断路器守的是内核自己的采样路径，
外部网关 / mesh 无法替内核决定「这一 turn 要不要发」。

## 决策

### 1. 状态挂装饰器实例（一个 endpoint 一个断路器）

`CircuitBreakingModelClient(inner, config=BreakerConfig())` 的状态机挂在 client 实例上，进程内所有
engine / turn / thread 共享同一份健康度。备选的 per-engine 被否：N 个 engine 会各自独立学一遍
「上游挂了」，正好抵消断路器要省的那些请求。

代价是作用域即「这个 client 实例」——同一进程连两个 endpoint 就该建两个装饰器实例，这也正是想要的隔离。
状态**不持久化**（进程内运行态，重启即闭合；多 worker 各自学习），理由见 Non-goals。

### 2. 只计一次 `stream` 的最终结局

断路器构造时对 inner 调 `with_default_retry`（ADR 0041 的幂等入口）：**保证自己叠在重试层之外**，
看到的每次失败都是「重试已耗尽」。不这么做的后果不是理论问题——业务侧若传裸 client，内核默认重试会
在引擎侧包在断路器**外层**（`Retrying(CircuitBreaking(native))`），`trip_after=3` 就从「3 次最终失败」
悄悄变成「3 次 attempt 失败」，即一次瞬时抖动就跳闸。

计入规则：

| 结局 | 计入 | 为什么 |
| --- | --- | --- |
| 可重试类 `LLMError`（`retryable=True`） | ✅ 连续计数 +1 | 重试已耗尽仍失败 = 上游确实不健康 |
| 不可重试类（鉴权 / 请求非法 / 内容拦截） | ❌ | 确定性终态，换个时间重试也一样，跳闸只会误伤 |
| `CancelledError` | ❌ | 用户意图，不是上游健康度 |
| 非 `LLMError`（编程错误等） | ❌ | 断路器不替代 bug 修复 |
| 正常完成 | 计数归零 | — |

判据用 `LLMError.retryable` 而非 `RetryConfig.retryable_kinds`：后者是「值不值得立刻重发」的白名单，
前者是「这个错误本质上是否瞬时」——断路器问的是后一个问题。

### 3. open 态抛新 kind `circuit_open`，而非复抛最近的真实错误

`CircuitOpenError(kind="circuit_open", retryable=True)`，`failure_class` 继承触发跳闸的那个错误
（如 `provider_transport`），并带 `retry_after_seconds` = 剩余冷却。

选它而不是复抛最近一次真实错误，是因为挂起 detail 要能区分两件事：「上游整体降级、N 秒后自动恢复」
与「本次调用失败」。复抛真实错误零新类型，但业务侧分不清自己是不是撞在熔断上，也就没法对用户
说「稍候自动恢复」而只能泛化成「模型调用异常」。

`retryable=True` 的含义是「上游恢复后重跑即可」→ `ConservativeFailurePolicy` 落 SUSPEND（不是硬失败）。
但 `circuit_open` **刻意不进** `RetryConfig.retryable_kinds` 默认集合：断路器打开期间重试只会撞回同一堵墙。
配 `failure_suspend_ttl_seconds` + `on_expire="retry"` 时，到期自动 retry 打在 open 态上不触网、成本≈0，
再配 `failure_suspend_max_auto_retries` 即有界。

### 4. 默认阈值 3 / 30s / ×2 / 300s，但默认不套

`BreakerConfig(trip_after=3, cooldown_seconds=30, cooldown_multiplier=2, max_cooldown_seconds=300)`。
`trip_after=3` 配 `max_attempts=3` 意味着跳闸前最多烧 9 次 attempt——足够区分「连着三个 turn 都栽」
与「偶发抖动」。半开只放行 **1** 个探测（其余并发继续快速失败），探测失败则冷却翻倍封顶，
避免长故障下反复空探测。

`EnginePool` **不默认套**（与 ADR 0041 的重试相反）：重试的默认值对所有部署都成立，而断路器的阈值
与「上游是什么」强相关——共享中转、自建 vLLM、多 endpoint 轮询的合理阈值差一个数量级。默认不套 ⇒
本 ADR 对既有部署零行为变化；业务侧显式包装即生效。

### 5. 三态各一个事件 kind

`provider_circuit_opened` / `provider_circuit_half_open` / `provider_circuit_closed`，经 session 可选协议
`set_circuit_observer` 上总线（探测风格完全复用 ADR 0039 的 `set_retry_observer`，宿主 `getattr` 探测、
透明包装靠 `__getattr__` 接通）。不共用一个带 `to_state` 字段的事件：运维按 kind 订阅跳闸告警更直接，
粒度也与既有的 `denial_circuit_open` 一致。OTel counter `taifeng.provider.circuit_transitions` 按
`to_state` / `failure_class` 计数。

## 后果

- 持续故障下的请求量从「每 turn 3 次」降到「每冷却窗口 1 次探测」；业务侧凭 `circuit_open` 可做
  用户可见的降级文案，凭三个事件可做告警与恢复时长看板。
- 断路器是**进程内**判断：多 worker 各自学习，各烧一遍跳闸成本；跨进程共享健康度需业务侧自建
  （见 Non-goals），内核不引入外部依赖。
- 误跳闸的代价是「冷却窗口内所有 turn 都挂起」。故意把不可重试类与取消排除在计数外，就是为了压低
  这个概率；阈值调低（如 `trip_after=1`）等于把这个风险交给接入方自己权衡。
- R1–R5：无业务概念（阈值全注入）；不涉压缩 / history（R2 不受影响）；三事件 + OTel 满足 R3；
  open 态快速失败不等待、半开探测走原 session 的 cancel（R4）；挂起 detail 带 `circuit_open`，
  resume 打回 open 态即再快速失败、有界（R5）。

## Non-goals

- **状态持久化 / 跨进程共享**：状态是进程内运行态，重启即闭合。跨进程要靠共享存储，等于给内核塞
  外部依赖（违反 ADR 0017 规则 ③：外部成熟服务能承担的，内核只定协议）。
- **自动故障转移到备用 endpoint**：内核不做 provider 选择；业务侧组合 `ModelClient` 自行实现。
- **做成 `EnginePool` 默认**：见决策 4。
- **改变 `RetryingModelClient` 的重试语义**：断路器只在其外层观察最终结局。
