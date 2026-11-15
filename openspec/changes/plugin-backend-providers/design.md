## Context

来源：计划 r2 的 Task 8（P2）。分支 `feat/plugin-backend-providers`。**开工条件：有具体宿主提出需求。** 没有的话保持本变更为提案状态。

## Goals / Non-Goals

目标是让一个插件能成套提供存储后端，且冲突永远显式。不改变直接传参的现有用法。

## Open Question（开工前必须先定）

`EnginePool.create` 的现状是：先建 store / directory / `HookRunner`，再 `cls(...)` 构造 pool；`plugin-core` 把插件加载点定在 `cls(...)` 之后。后端提供者必须在建 store 之前可用，两者时序冲突。候选方案：

- **A. 两段加载**：新增可选的 `Plugin.provide(ctx)` 阶段，在建 store 之前只收集后端提供者；`setup(ctx)` 仍在原加载点。两段都参与回滚。
- **B. 加载点整体前移**：所有插件在建 store 之前 setup。问题是此时工具注册表和 skill 注册表的就绪顺序要重排，`ctx.cancel` 所依赖的 `_root_cancel` 也还不存在。

倾向 A：对已交付的四个 capability 无影响，只新增一个阶段。定案后写新 ADR 并更新 `plugin-core` 契约。

## Decisions

1. **单一提供者**。每个 capability 每个 pool 至多一个 factory。
2. **冲突一律报错**。两个插件提供同一 capability，或宿主已显式传了 `message_store=` / `thread_directory=` / `audit=` 而插件又提供，抛 `PluginCapabilityError`。不采用「显式参数优先、插件被忽略」，那是仓库禁止的静默回退。
3. **冻结**。提供阶段结束后提供者表冻结，运行中不可替换。
4. **所有权**。插件提供的 store 由 pool 拥有并在 `close()` 时关闭，与今天注入 `message_store=` 的语义一致；关闭顺序在插件 close 之后（store 最后关）。
5. **校验不绕过**。插件提供的 store 走与显式注入相同的 `validate_injected_store` 校验；外部 journal 走现有 `SessionJournalCore` conformance 测试。
6. **审计边界**。只有 `provide_journal_core` 能参与审计装配；普通 capability 不能拿到 lease。

## Risks

- 方案 A 让插件协议多一个可选方法，加载失败的回滚路径多一个分支。
- 恢复时漏加载提供者插件会表现为「历史为空」而不是报错；需要评估是否在 store 层加一个提供者标识校验。
