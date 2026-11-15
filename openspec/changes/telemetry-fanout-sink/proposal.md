## Why

`EnginePool.create(sink=...)` 只接受一个 `TelemetrySink`，仓库里没有任何多 sink 分发机制。插件要观察事件，就必须有一个可增删成员的 sink。

## What Changes

- 新增 `telemetry/fanout.py` 的 `FanoutSink`：可增删成员，成员之间故障隔离。
- 仅当传了 `plugins` 时，pool 用 `FanoutSink` 包住宿主的 `sink=`。
- `PluginContext` 增加 `telemetry` capability。
- 在契约里写清插件 sink 第一版能收到哪些事件。

## Capabilities

### New Capabilities

- `telemetry-fanout-sink`：多 sink 分发与插件 sink 注册。

### Modified Capabilities

无。

## Non-goals

不把 engine 的 turn / tool 事件流转发给插件 sink（那一路今天由宿主经 `engine.subscribe_all()` 获取）；不做 sink 级过滤、缓冲或重试；不改 `TelemetrySink` 协议。

## R1–R5 影响

- R1：无业务字段。
- R2：不涉及。
- R3：这是 R3 的投递面；成员失败只记日志，不丢其他成员的事件。
- R4：`handle` 逐个 await 成员，成员应自行保证不长时间阻塞；fan-out 本身不引入新等待点。
- R5：不涉及。

## Impact

实现：`src/taifeng/telemetry/fanout.py`、`src/taifeng/plugin/context.py`、`src/taifeng/loop/pool_plugins.py`。

测试：`tests/telemetry/test_fanout.py`、`tests/plugin/test_context.py`、`tests/loop/test_pool_plugins.py`。

文档：`docs/architecture/capabilities/plugin-system.md`。

`FanoutSink` 本身（任务 1）不依赖 `plugin-core`，可并行开工；pool 接线（任务 2）依赖它。
