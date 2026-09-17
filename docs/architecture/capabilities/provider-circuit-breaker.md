# Capability: provider-circuit-breaker

## Purpose

上游（provider / 中转）**持续性**故障时，让内核跨 turn 记住健康度：连续 N 次「重试已耗尽」的最终失败即跳闸，此后在冷却窗口内不再触网，直接快速失败并给业务侧一个可识别的「上游已降级」信号。

与既有两层的分工：`RetryingModelClient` 管「这一次调用抖了一下」（turn 内、有界重试）；`DenialBreaker` 管「这个 turn 里模型被连续拒绝」（turn 内、权限语义）。本能力管「这个 endpoint 整体挂了」——**跨 turn、跨 engine、进程内共享**，是三者中唯一有跨 turn 记忆的。

参照：claw-code `denial_breaker` 的三态骨架（差异：那边计 turn 内权限拒绝，这里计 provider 最终失败且跨 turn 生效）。

实现：`src/taifeng/llm/breaker.py`（`BreakerConfig` / `CircuitState` / `CircuitTransition` / `_Circuit` / `_BreakerSession` / `CircuitBreakingModelClient`）、`src/taifeng/llm/errors.py`（`CircuitOpenError`）、`src/taifeng/loop/event.py`（三个事件类）、`src/taifeng/loop/turn_sample.py`（观察者接线）、`src/taifeng/telemetry/{console,otel_sink}.py`。
决策记录：[ADR 0042](../../decisions/0042-provider-circuit-breaker.md)。变更提案：`openspec/changes/provider-circuit-breaker/`。

## 数据契约

### `BreakerConfig`（`@dataclass(frozen=True)`）

| 字段 | 类型 | 默认 | 语义 |
| --- | --- | --- | --- |
| `trip_after` | `int` | `3` | 连续多少次「最终失败」跳闸 |
| `cooldown_seconds` | `float` | `30.0` | 首次跳闸后的冷却时长（秒） |
| `cooldown_multiplier` | `float` | `2.0` | 半开探测再失败时冷却的增长倍数 |
| `max_cooldown_seconds` | `float` | `300.0` | 冷却上限，防止长故障把冷却放大到不可恢复 |

默认值只在业务侧**显式包装**时生效——内核不默认套断路器（ADR 0042 决策 4）。

### `CircuitState`（`StrEnum`）

`CLOSED` / `OPEN` / `HALF_OPEN`，值分别为 `"closed"` / `"open"` / `"half_open"`（StrEnum 保证事件 data 内序列化稳定）。

### `CircuitTransition`（`@dataclass(frozen=True)`）

纯数据，不依赖 loop 层事件类型；宿主据此 emit 事件。

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `from_state` / `to_state` | `str` | 转换前 / 后状态 |
| `consecutive_failures` | `int` | 转换时的连续最终失败计数 |
| `cooldown_seconds` | `float` | 转换后生效的冷却时长 |
| `last_failure_class` | `str \| None` | 最近一次**计入**的失败的稳定 failure_class；从未失败则 `None` |
| `last_error_kind` | `str \| None` | 最近一次计入的失败的异常类名 |

### `CircuitOpenError`（`llm/errors.py`，`kind="circuit_open"`）

`retryable=True`；`failure_class` 继承触发跳闸的错误；`retry_after_seconds` = 剩余冷却秒数。
**不在** `RetryConfig.retryable_kinds` 默认集合内（打开期间重试只会撞回同一堵墙，见 ADR 0042 决策 3）。

### 三个事件（`loop/event.py`）

`provider_circuit_opened` / `provider_circuit_half_open` / `provider_circuit_closed`，`data` 为 `CircuitTransition` 各字段加 `iteration`（采样圈序号，1-based）。

### 可选协议 `set_circuit_observer(observer)`（session 级）

`observer: Callable[[CircuitTransition], Awaitable[None]]`。宿主用 `getattr(session, "set_circuit_observer", None)` 探测——未套断路器的 session 无此方法即静默跳过；透明包装靠 `__getattr__` 转发接通。探测风格与 `set_retry_observer`（ADR 0039）完全一致。

## 行为契约

### Requirement: 断路器只计一次 `stream` 的最终结局
`CircuitBreakingModelClient` 构造时对 inner 调 `with_default_retry`（幂等），保证自己叠在重试层之外。计入规则：可重试类 `LLMError`（`retryable=True`）计入；不可重试类、`CancelledError`、非 `LLMError` 均不计；正常完成则计数归零。

#### Scenario: 内层重试吸收的 attempt 不计
- **WHEN** 一次 `stream` 内前两次 attempt 失败、第三次成功
- **THEN** 断路器视为**一次成功**，连续失败计数归零

#### Scenario: 重试耗尽只记一次
- **WHEN** 一次 `stream` 的 3 次 attempt 全失败
- **THEN** 连续失败计数只 +1（而非 +3）

### Requirement: 连续失败跳闸
- 连续 `trip_after` 次最终失败 → 状态转 `open`，emit `provider_circuit_opened`，冷却为 `cooldown_seconds`。

### Requirement: open 态快速失败
- **WHEN** 状态为 `open` 且未到冷却期
- **THEN** `stream` **不发起任何网络请求**，立即抛 `CircuitOpenError`

### Requirement: 半开只放行一个探测
- **WHEN** `open` 态经过冷却时长后的首次 `stream`
- **THEN** 状态转 `half_open`（emit `provider_circuit_half_open`），该次调用即探测；探测在途期间其余并发 `stream` 继续快速失败

### Requirement: 探测成功闭合 / 失败重开
- 探测正常完成 → `closed`，计数与冷却清零，emit `provider_circuit_closed`。
- 探测失败 → `open`，冷却 `×cooldown_multiplier` 并封顶 `max_cooldown_seconds`，emit `provider_circuit_opened`。

### Requirement: 与保守失败策略配合
- **WHEN** turn 采样抛 `CircuitOpenError` 且未注入 failure_policy
- **THEN** 落 SUSPEND（`SuspendReason.SYSTEM_RETRY`），挂起 `detail["kind"] == "CircuitOpenError"`、`detail["failure_class"]` 为触发跳闸的病根分类

### Requirement: 作用域与生命周期
- 状态挂在 client 装饰器实例上，进程内所有 engine / turn / thread 共享；**不持久化**，重启即闭合，多 worker 各自学习。

## 组合与接线

```python
from taifeng.llm import BreakerConfig, CircuitBreakingModelClient

# 推荐：断路器在最外层；inner 未套重试时构造函数会自动补（幂等）
client = CircuitBreakingModelClient(
    OpenAICompatClient(...),
    config=BreakerConfig(trip_after=3, cooldown_seconds=30.0),
)
pool = await EnginePool.create(skills_dir=..., threads_dir=..., model_client=client)
```

- 引擎侧的默认重试（ADR 0041）探测到断路器转发出的 `bounded_retry` 标记即跳过外层包装，断路器因此稳居最外层。
- `clock` 参数（默认 `time.monotonic`）供测试注入假时钟验证冷却窗口，生产不传。
- 多 endpoint ⇒ 多个装饰器实例，各自独立计健康度。

## 边界与已知限制

- **误跳闸代价**：冷却窗口内该 endpoint 上所有 turn 都会挂起。把不可重试类与取消排除在计数外正是为压低这个概率；调低 `trip_after` 等于把风险交给接入方权衡。
- **进程内判断**：多 worker 各烧一遍跳闸成本；跨进程共享健康度属业务侧职责（ADR 0042 Non-goals）。
- **与 strict audit 互斥的传递**：inner 为 `AttemptObservableModelClient` 时 `with_default_retry` 原样返回（audit 优先），此时断路器计的是单次 attempt——审计模式本就与自动重试互斥（ADR 0037）。
