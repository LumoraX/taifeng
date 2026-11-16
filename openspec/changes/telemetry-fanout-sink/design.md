## Context

来源：计划 r2 的 Task 6。分支 `feat/telemetry-fanout-sink`。r1 写的是「不改变现有 attach API」，但现状是 `telemetry/sink.py` 只有一个单方法协议，没有 attach API。

另一个核对到的事实：pool 的 `sink=` 今天只收 **pool 级事件**（索引钩子失败、持久化层事件），不是 engine 的 turn / tool 事件流。

## Goals / Non-Goals

目标是让多个 sink 共享 pool 级事件，互不影响。不改变未使用插件时的任何对象身份或行为。

## Decisions

1. **`FanoutSink(TelemetrySink)`**。`add(sink) -> RegistrationHandle`；`handle(ev)` 按加入顺序依次 await 成员。
2. **故障隔离**。单个成员抛错只记日志并继续，`handle` 不向上抛——上游（`HookRunner` 等）不应因观察者失败而失败。`CancelledError` 例外，照常传播。
3. **重复加入报错**。同一对象二次 `add` 抛 `ValueError`。
4. **迭代快照**。`handle` 对成员列表取副本再迭代，投递中增删不影响本次投递。
5. **只在有插件时包装**。传了 `plugins` 时，`pool_plugins` 构造 `FanoutSink`，宿主 `sink` 排第一个，再把它交给原有使用方（`HookRunner`、`SqliteThreadDirectory`）。未传 `plugins` 时 `sink` 原样透传，对象身份不变。
6. **时序**。sink 的使用方在 `cls(...)` 之前就拿到 sink 了，而插件在其后加载。所以 `FanoutSink` 要在 `create` 开头就建好并传下去，插件加载时再往里加成员。这是本变更对 `create` 流程唯一的前置改动。
7. **第一版范围**。插件 sink 收到的是 pool 级事件，外加 `plugin-core` 的三个生命周期事件。engine 事件流的转发留到第二阶段：每个 engine 多一个订阅者，涉及背压和 seq 语义，需要单独设计。

## Risks

- 决策 6 让 `FanoutSink` 的创建早于 manager；加载失败时它没有需要清理的资源，但要确认失败路径上不遗留成员。
- 第一个生命周期事件 `plugin_loaded` 发出时，后加载的插件 sink 还没加入，它们收不到更早插件的 `plugin_loaded`。契约要写明「sink 只收到自己加入之后的事件」。
