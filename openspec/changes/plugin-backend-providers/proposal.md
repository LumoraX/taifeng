## Why

宿主今天通过 `message_store=`、`thread_directory=`、`audit=` 直接注入存储后端。若一组后端总是成套出现，把它们打包成一个插件会更省事。这是便利性需求，不是机制缺口，所以排在最后，**有真实宿主需求再做**。

## What Changes

- `PluginContext` 增加 `backends` capability：`provide_message_store`、`provide_thread_directory`、`provide_journal_core`。
- 每个 capability 每个 pool 只允许一个提供者；与显式参数或其他插件冲突一律报错。
- 调整插件加载时序，使后端提供者在 store 构建之前可用。

## Capabilities

### New Capabilities

- `plugin-backend-providers`：由插件提供存储后端。

### Modified Capabilities

- `plugin-core`：加载流程拆成两段或加载点前移（方案在本变更的设计阶段定）。

## Non-goals

不在内核里实现任何具体后端；不允许运行中替换持久化真相源；不放宽 `AuditConfig` 对 lease 的控制；不改 `MessageStore` / `ThreadDirectory` / `SessionJournalCore` 协议。

## R1–R5 影响

- R1：后端实现全在宿主侧。
- R2：不涉及。
- R3：沿用既有持久化层事件。
- R4：factory 调用受 `ctx.cancel` 约束。
- R5：**直接相关**。存储是 resume 的真相源，提供者在 setup 结束后冻结；恢复时宿主必须加载同一个提供者插件，否则读不到历史。

## Impact

实现：`src/taifeng/plugin/context.py`、`src/taifeng/plugin/manager.py`、`src/taifeng/loop/{pool_plugins,pool}.py`。

测试：`tests/plugin/test_backend_capabilities.py`、`tests/loop/test_audit_external_journal_core.py`。

文档：`docs/architecture/capabilities/plugin-system.md`、`docs/architecture/conversation.md`、新 ADR（修改了 0144 定下的加载点）。

触及持久化装配与 `loop/`，合入前须跑真实回归。前置：`plugin-core`。
