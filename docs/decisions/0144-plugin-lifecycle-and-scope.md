# ADR 0144：插件生命周期与作用域

- 状态：Accepted
- 日期：2026-11-15
- 关联：[plugin-system 能力契约](../architecture/capabilities/plugin-system.md)、[ADR 0017 立项裁决](0017-kernel-positioning-criteria.md)、[ADR 0066 公共 API 分层](0066-public-api-tiers-and-deprecation.md)

## 背景

宿主扩展 Taifeng 今天只有两条路：在 `EnginePool.create` 上传越来越多的参数，或改内核。前者让
`create` 的参数表持续膨胀（已六十余个），后者违反 R1。宿主需要把「一组工具 + 钩子 + skill +
观测」打包成一个单元，随 pool 一起装配和拆除，且拆除后不残留。

这是内核机制缺口（ADR 0017 规则①）：生命周期、回滚、取消与可观测必须由内核保证，宿主无法在外面
正确地拼出来——比如 setup 失败时谁来撤销已注册的工具。

## 决策

1. **显式注入，不做发现**。只接受 `EnginePool.create(plugins=...)`。不扫描 entry points、不隐式 import：
   插件来源与信任由宿主决定，内核不替宿主决定加载什么。
2. **capability allowlist**。插件只能调用 `PluginContext` 上显式提供的注册方法，拿不到 engine、runner、
   prompt 或 history。新 capability 逐个加入并各自定义冲突与撤销语义，而不是给一个通用的
   `register(name, obj)`——后者无法定义撤销与冲突。
3. **一套清理路径**。`setup` 不返回 handle；context 把每个注册登记到 manager，由 manager 逆序撤销。
   插件的 `close()` 可选，只做插件自己的收尾。两套路径（返回 handle + 插件 close）会让「谁负责撤销」
   含糊，回滚时容易漏或重复。
4. **第一版只在 pool 关闭时卸载**。关闭点在全部 Session 强制释放之后，此时不存在在途调用，因此不需要
   在 `ToolCallRuntime` 里跟踪在途调用。运行中卸载单个插件留到第二阶段，届时再加在途计数。
5. **只有 `pool` 作用域**。`engine` / `session` 作用域需要每个 engine 的独立注册表与所有权测试，第一版
   不预留半成品接口。
6. **事件走 pool 级 sink**。插件在首个 engine 之前加载，没有 engine 事件总线可用；沿用持久化层事件的
   先例，以 `submission_id="*"`、`seq=0` 直接交给 `sink=`。
7. **核心先行，capability 分期**。本 ADR 落地的核心不含任何 capability；tool / hook、skill 目录、
   pool 级 sink fan-out、存储后端各自独立变更交付。

## 否决的方案

- **全局插件图 / 热重载**：引入跨 pool 的共享状态，与「两个 pool 互不泄漏」冲突，且 R2 下热重载会
  反复打破 prompt 缓存。
- **加载失败时跳过失败插件、继续启动**：属于静默回退；宿主以为某能力在线而实际不在。
- **把冲突交给「显式参数优先」**：同上，插件以为自己注册成功而实际被忽略。冲突一律报错。

## 后果

- `EnginePool.create` 多一个参数；接线放在新模块 `loop/pool_plugins.py`，`loop/pool.py` 只做透传，
  守住 800 行红线。
- 新增 `plugin_loaded` / `plugin_failed` / `plugin_unloaded` 三个事件 kind。
- 插件 API 在实验层，第二阶段（运行中卸载、更多作用域）稳定后再评估晋升。
- 改动了基础层 `loop/`，合入前按红线跑真实回归并更新台账。
