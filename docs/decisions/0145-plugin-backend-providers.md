# ADR 0145：插件提供存储后端——两段加载

- 状态：Accepted
- 日期：2026-11-15
- 修订：[ADR 0144](0144-plugin-lifecycle-and-scope.md) 决策 7（加载点）——新增一个更早的声明段，setup 段不变
- 关联：[plugin-system 能力契约](../architecture/capabilities/plugin-system.md)

## 背景

宿主今天用 `message_store=`、`thread_directory=`、`audit=` 注入存储后端。一组后端若总是成套出现，打包成插件更省事。
但 ADR 0144 把插件 `setup` 放在 pool 构造之后——那时 store、目录和审计配置早已就位，插件没有机会提供它们。

## 决策

1. **两段加载**。插件可选实现**同步**方法 `provide_backends(backends)`，只声明工厂、不做 IO；`EnginePool.create`
   在构造任何后端之前收集声明并调用工厂。`setup(ctx)` 仍在原加载点。对只用 tool / hook / skill / sink 的插件
   没有任何变化。
2. **提供整份 `AuditConfig`，不单独提供 journal core**。journal core 离不开 `writer_id` 与附件上限，单给一个 core
   pool 无法装配审计。可提供的后端是 `message_store`、`thread_directory`、`audit` 三项。
3. **单一提供者、冲突报错**。同一后端两个插件都提供，或宿主已显式传了对应参数（`message_store` 还包括
   `storage_dir` / `threads_dir`）而插件又提供，一律 `PluginCapabilityError`。不采用「显式参数优先」——那是静默忽略。
4. **所有权与宿主注入对齐**。插件提供的 store 由 pool 拥有：`create` 中途失败时由内核关闭（宿主注入的 store 在
   `create` 成功前仍归宿主，不变）；成功后与注入的 store 一样在 `pool.close()` 时关闭，且晚于插件自身的 close。
   插件提供的 thread 目录与宿主注入的一样不由 pool 关闭。
5. **校验不绕过**。插件 store 走与宿主注入相同的 `validate_injected_store`；插件 `AuditConfig` 走同一个审计静态门。
   审计 pool 照旧拒绝插件注册工具 / 钩子 / skill 目录。
6. **声明即冻结**。`provide_backends` 返回后登记器封口；运行中不可替换持久化真相源。

## 否决的方案

- **把整个加载点前移到建 store 之前**：工具注册表、skill 注册表与 pool 根取消令牌那时都还不存在，已交付的四个
  capability 要全部改时序。
- **异步 `provide_backends`**：声明段不需要 IO；保持同步可以不引入「pool 还没有根令牌时如何取消」的问题。

## 后果

- 插件协议多一个可选方法；`EnginePool.create` 在最开头多一步后端解析（`loop/pool_plugins.resolve_plugin_backends`）。
- 恢复会话时宿主必须加载**同一个**提供者插件，否则读不到历史——与「换了 `message_store=` 就读不到」同理，
  契约里写明。
- 触及持久化装配，合入前按红线跑真实回归。
