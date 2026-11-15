## Why

业务系统今天要扩展 Taifeng，只能在 `EnginePool.create` 上堆参数或直接改内核。缺一个受内核生命周期、取消和可观测性约束的扩展入口，让宿主把扩展写成插件包。

## What Changes

- 新增 `src/taifeng/plugin/` 包：`PluginSpec`、`Plugin`、`RegistrationHandle`、`PluginContext`、`PluginManager` 和错误集合。
- manager 按依赖拓扑加载，setup 失败逆序回滚，close 逆序撤销且有超时上限。
- 新增三个生命周期事件 `plugin_loaded` / `plugin_failed` / `plugin_unloaded`。
- `EnginePool.create(plugins=...)` 显式注入；接线放新模块 `loop/pool_plugins.py`。
- 本变更只交付**核心**：`PluginContext` 上此时只有 `plugin_id` 和 `cancel`，具体 capability 由后续变更逐个加上。

## Capabilities

### New Capabilities

- `plugin-core`：插件身份、依赖、生命周期、事件和 pool 接线。

### Modified Capabilities

无。

## Non-goals

不做自动发现（entry points）、热重载、运行中卸载单个插件、`engine` / `session` scope；不支持不经 pool 直接构造的 `AgentEngine`；不含任何 tool / hook / skill / sink / 存储 capability。

## R1–R5 影响

- R1：`src/taifeng/plugin/` 只含通用机制，无业务字段、无 `os.getenv`。
- R2：本变更不注册任何进 prompt 的东西，不触碰压缩与缓存。
- R3：三个生命周期事件经 pool 级 sink 发出。
- R4：`ctx.cancel` 由 pool `_root_cancel.child()` 派生；setup 期间取消触发回滚；close 有超时。
- R5：插件不持久化状态；不改 `MessageStore`。

## Impact

实现：`src/taifeng/plugin/{errors,types,context,manager,__init__}.py`、`src/taifeng/loop/{pool_plugins,pool,pool_lifecycle,event_base,event_lifecycle,event}.py`、`src/taifeng/telemetry/{console,otel_sink}.py`、`src/taifeng/experimental.py`。

测试：`tests/plugin/{test_context,test_manager}.py`、`tests/loop/test_pool_plugins.py`、`tests/telemetry/test_plugin_events.py`。

文档：新增 `docs/architecture/capabilities/plugin-system.md`、`docs/decisions/0144-plugin-lifecycle-and-scope.md`；其余活文档同步归 `plugin-isolation-and-release`。

改动 `src/taifeng/loop/`（基础层），合入前须跑真实回归并更新台账。
