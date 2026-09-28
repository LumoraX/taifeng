# Capability: model-routing-composition（组合契约）

## Purpose

多模型路由（按请求选模型 / provider）与失败回退（主模型不可用时换备用）属于 **userspace**：内核不内置路由表、价目或回退链
（ADR 0017 规则④）。但它们都要以 `ModelClient` 包装器的形式插进内核，包装得不对会静默破坏内核的保证（重试语义、缓存命中、
thinking 回传、审计摘要）。本契约规定包装器必须遵守的组合规则，让业务写的路由 / 回退与内核的既有包装（`RetryingModelClient`、
`CircuitBreakingModelClient`）正确叠加。

状态：契约（无内核实现）。关联：[provider-circuit-breaker](provider-circuit-breaker.md)、[llm-provider-native](llm-provider-native.md)、ADR 0067。

## 组合顺序

```
业务 Router / Fallback（选 endpoint）
  └─ CircuitBreakingModelClient（每个 endpoint 一个，跨 turn 记健康度）
       └─ RetryingModelClient（每个 endpoint 一个，turn 内有界重试）
            └─ 具体 provider client
```

- 回退 SHALL 发生在断路器**之外**：先由该 endpoint 自己的重试与断路器消化瞬时故障，只有「重试耗尽的最终失败」或
  `CircuitOpenError` 才触发换 endpoint。反过来包（回退在重试之内）会让一次抖动就切模型，破坏缓存与对话一致性。
- 每个 endpoint 独立的断路器实例；多个 endpoint 共用一个断路器会让 A 的故障把 B 也熔断。

## Requirements

### Requirement: 只在零产出时回退
回退 SHALL 仅在本次 attempt **尚未向上游产出任何 `ResponseEvent` 内容**（无 text / reasoning / tool_call 增量）时发生，
与 `RetryingModelClient` 的「零产出才可安全重发」判据一致（ADR 0037）。已产出部分内容后失败 SHALL 原样上抛，
由 turn 层失败处置（ADR 0039）接管——中途换模型会把两个模型的半截输出拼在一起。

### Requirement: 按 failure_class 决定是否回退
| `failure_class` | 回退 |
| --- | --- |
| `provider_transport` / `provider_internal` / `provider_rate_limit` / 断路器 `CircuitOpenError` | 可回退 |
| `context_window` | 仅当备用 endpoint 的窗口更大时可回退；否则上抛交给压缩自愈 |
| `invalid_request` / `provider_auth` / `request_size` | SHALL NOT 回退（换模型也不会成功，且会掩盖配置错误） |
| `content_filter` | SHALL NOT 静默回退（绕过安全拦截属于业务决策，须显式配置并留痕） |
| `cancelled` | SHALL NOT 回退 |

### Requirement: 能力一致
包装器对外声明的 `capabilities`（`ModelCapabilities`）SHALL 是全部候选 endpoint 能力的**交集**：若主模型支持图片输入而备用不支持，
包装器不得声明图片能力，否则回退后请求会在备用 provider 处失败或被静默降级。`protocol`（chat / responses）不同的 endpoint
SHALL NOT 放进同一回退组：历史里的 provider state（Responses reasoning items、Anthropic thinking 签名、Gemini thoughtSignature）
只对产生它的协议有效。

### Requirement: 可观测
每次路由决定与回退 SHALL 可观测：`server_model` 事件如实上报实际服务的模型（内核据此做 cache break 归因的 `model_changed`）；
回退发生时宿主应 emit 自有事件或日志，写明原 endpoint、failure_class 与新 endpoint。

### Requirement: 缓存与审计影响如实承担
换模型必然使 prompt cache 失效（`cache_break_detected` 归因 `model_changed`，属预期内）。strict audit 模式下请求摘要含 provider 与
model，回放（journal-replay）按录制时的组合匹配——路由结果不确定的包装器在回放时会分叉，这是如实信号而非缺陷。

## 非目标
内核不提供路由表、成本模型、健康探测调度或默认回退链；这些由业务按上述规则实现。
