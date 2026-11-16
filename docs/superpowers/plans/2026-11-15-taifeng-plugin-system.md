# Taifeng Kernel Plugin System Implementation Plan（修订版 r2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Taifeng 增加一个受内核生命周期、作用域、取消和可观测性约束的插件扩展层，让业务系统实现插件包而不是修改 Taifeng 内核。

**Architecture:** Taifeng 提供显式注入的 `PluginManager`、`PluginContext` 和可逆 `RegistrationHandle`。第一版只支持 `EnginePool` 级（`scope="pool"`）插件，插件通过受限注册 API 接入现有 `ToolRegistry`、`HookRegistry`、skill 目录、pool 级 `TelemetrySink`，P2 再接存储后端；插件不能拿到 actor、prompt、history 或业务对象。未传 `plugins` 时启动路径与当前行为完全一致。

**Tech Stack:** Python 3.12+, anyio, frozen dataclass / Protocol, Pydantic EventMsg, pytest asyncio。

---

## 修订记录（r1 → r2，对照 `origin/main@ea3b36b` 核对）

r1 有以下问题，r2 已逐条改掉。实现时以 r2 为准。

| # | r1 的问题 | r2 的处理 |
| --- | --- | --- |
| 1 | ADR 编号 0143 已被 `0143-stable-execution-identity.md` 占用 | 改为 **0144** |
| 2 | `loop/pool.py` 已 784 行，再加代码会破 800 行硬红线 | 插件接线全部放新模块 `loop/pool_plugins.py`，`pool.py` 只加参数透传 |
| 3 | 把 `SkillSource` 当成可插拔对象；它其实是信任层级 `Literal["system","user","marketplace"]`，注册表也不支持运行期增删目录 | 新增 Task 5：给 `FilesystemSkillRegistry` 加可撤销的 `add_dir` |
| 4 | 「不改变现有 attach API」——仓库里没有多 sink / attach API；且 pool 的 `sink=` 只收 hook 失败与持久化层事件，不是 engine 事件流 | 新增 Task 6：`FanoutSink`，并写明插件 sink 第一版能收到哪些事件 |
| 5 | `HookRegistry` 只有 `register`，没有撤销；架构段承诺接 hook 但没有任何 Task 实现 | Task 4 增加 `HookRegistry.unregister` 与 `ctx.hooks` |
| 6 | 「运行中的 tool 不可拔除」无法实现：`ToolCallRuntime` / `ToolRegistry` 不跟踪在途调用 | 第一版 handle 只由 manager 在 pool close 时关闭，届时 Session 已全部释放；删除 `PluginActiveRegistrationError` |
| 7 | Task 3 测试用了不存在的 `engine.tool_registry` | 改为 `pool.tool_registry` |
| 8 | Task 2 就要发 `plugin_failed`，事件类却在 Task 4 才建；`rollback_errors` 不在字段表里 | 事件类提前到 Task 2，字段表补 `rollback_errors` |
| 9 | 冲突规则自相矛盾（Task 3 说报错，Task 6 说显式参数优先） | 统一为「冲突一律报 `PluginCapabilityError`，不静默覆盖也不静默忽略」 |
| 10 | pool close 顺序写成「等待 active work」；实际 `_drive_pool_close` 是强制释放全部 Session | 按真实顺序写明插件 close 的位置 |
| 11 | 会话恢复时插件缺席的行为未定义（R5） | Task 3 / Task 5 各加一条契约与测试 |
| 12 | 插件事件要加到 717 行的 `event_turn.py`，且漏了 `event_base.py` 的 `MsgKind` | 放 `event_lifecycle.py`，同步 `MsgKind` |
| 13 | `setup(ctx)` 没有取消令牌，R4 落不了地 | `ctx.cancel` 由 pool 根令牌 `child()` 派生 |
| 14 | `Plugin.close()` 与 `setup()` 返回的 `PluginHandle` 两套清理路径 | 只留一套：`setup` 不返回值，manager 负责逆序关闭 handle；`Plugin.close` 为可选收尾 |
| 15 | Task 1 测试用通用 `ctx.register("demo-capability", object())`，与 capability allowlist 相悖 | 测试改用真实 capability（`ctx.tools.register`） |
| 16 | 预留了 `PluginContext.child(scope="engine")` 半成品接口 | 删除，第二阶段再设计 |
| 17 | 漏同步 `configurable-knobs.md`、`capabilities/README.md`、`overview.md`、`skill-system.md`，缺 R1–R5 影响声明 | Task 9 补齐，下方新增「R1–R5 影响声明」 |
| 18 | 未提 `mcp/bridge.py` 这个既有的运行期工具注册先例 | Task 4 要求沿用其约定并定义重名规则 |

### 待确认的两项默认（已按推荐写入，改主意只影响标注的变更）

- **卸载时机**：第一版 handle 只在 pool close 时由 manager 关闭，不支持运行中卸载单个插件。要支持的话需给 `ToolCallRuntime` 加在途调用计数（影响 `plugin-core`、`plugin-tool-hook-registration`）。
- **skill 重名**：插件目录里的 skill id 与已有 skill 重名时报错，不沿用现有「后加载覆盖先加载 + 告警」（影响 `skill-dir-registration`）。

## 设计边界

### 内核必须提供

- 插件身份、版本、依赖和作用域声明。
- 依赖拓扑排序、重复 ID 检查、缺依赖与循环依赖拒绝。
- setup 失败时反向回滚；close 时按逆依赖顺序撤销注册。
- `CancellationToken` 传播（`ctx.cancel`）、异步 setup、有界 close 超时。
- 每个注册动作返回可逆 `RegistrationHandle`，pool 关闭后不残留 tool、hook、skill 目录或 sink。
- `plugin_loaded`、`plugin_failed`、`plugin_unloaded` 三个 EventMsg。
- capability allowlist：插件只能调用 `PluginContext` 上显式提供的注册方法。

### 业务层必须提供

- 业务工具、业务 skill、业务 prompt、外部系统连接器。
- 业务权限策略、审批文本、UI、外部 API 凭据和数据转换。
- 具体的 `MessageStore`、`ThreadDirectory`、`IndexHook`、`SessionJournalCore` 实现。

### 本计划明确不做

- 不做全局插件图、profile / bundle / 热重载体系。
- 不扫描 Python entry points，不隐式 import 插件；只接受 `EnginePool.create(plugins=...)` 显式注入。
- 不把 `tenant_id`、宿主业务名、业务审批术语或具体 UI 放入 `src/taifeng/plugin/`。
- 不允许插件直接改 prompt / history。
- 第一版不支持运行中卸载单个插件，不支持 `engine` / `session` scope，不支持不经 pool 直接构造的 `AgentEngine`。

## R1–R5 影响声明

- **R1 业务零侵入**：`src/taifeng/plugin/` 只含通用机制；Task 7 加静态测试扫描业务词与 `os.getenv`。
- **R2 Cache 友好**：工具集与 skill 列表都进 prompt 前缀。插件在首个 engine 创建前注册，不影响任何已有缓存；pool close 时才撤销，届时已无活跃 Session。运行期调用 `add_dir` / 撤销会使活跃会话的前缀缓存失效一次，契约文档必须写明。不涉及压缩策略，不改 `CompressionResult`。
- **R3 可观测**：三个生命周期事件走 pool 级 sink；工具集变更继续走既有 `tool_set_changed`。
- **R4 可取消**：`ctx.cancel` 由 pool `_root_cancel.child()` 派生；setup 期间取消触发回滚；close 有超时上限。
- **R5 可 resume**：插件不持久化任何状态。恢复含插件工具调用的会话而插件缺席时，沿用现有行为：`ToolCallRuntime` 返回 `unknown_tool` 错误结果（`reason="not_in_registry"`），不崩溃。入口 skill 来自缺席插件目录时的现有行为尚未核实，Task 5 先写测试固定现状，再写进契约。

## 拆分后的 spec（实施以这里为准）

本计划已拆成六个 OpenSpec 变更，各自带 `proposal.md`（为什么、范围、R1–R5、影响面）、`design.md`（决策与风险）、`tasks.md`（文件、步骤、命令、验收）和 `specs/<capability>/spec.md`（需求与场景）。本文件只保留总览；实施细节不在这里重复。

| 顺序 | 变更 | 交付 | 前置 | 对应 r2 Task |
| --- | --- | --- | --- | --- |
| 1 | [`plugin-core`](../../../openspec/changes/plugin-core/) | 契约、ADR 0144、生命周期事件、manager、`EnginePool.create(plugins=...)` | 无 | 1–3 |
| 2a | [`plugin-tool-hook-registration`](../../../openspec/changes/plugin-tool-hook-registration/) | `ctx.tools`、`ctx.hooks`、`HookRegistry.unregister` | 1 | 4 |
| 2b | [`skill-dir-registration`](../../../openspec/changes/skill-dir-registration/) | `FilesystemSkillRegistry.add_dir`、`ctx.skills` | 注册表部分无；接线需 1 | 5 |
| 2c | [`telemetry-fanout-sink`](../../../openspec/changes/telemetry-fanout-sink/) | `FanoutSink`、`ctx.telemetry` | `FanoutSink` 本身无；接线需 1 | 6 |
| 3 | [`plugin-isolation-and-release`](../../../openspec/changes/plugin-isolation-and-release/) | 隔离测试、示例、文档同步、真实回归 `plugin_tool` | 1、2a、2b、2c | 7、9 |
| 4 | [`plugin-backend-providers`](../../../openspec/changes/plugin-backend-providers/) | 插件提供存储后端（有宿主需求再做） | 1 | 8 |

并行方式：`plugin-core` 开工的同时，`skill-dir-registration` 的注册表部分和 `telemetry-fanout-sink` 的 `FanoutSink` 可以各开一个 worktree 并行；2a / 2b / 2c 的接线部分都改 `plugin/context.py` 与 `loop/pool_plugins.py`，按「一次合一条分支」依次集成。

## 验收门槛

第一阶段（上表顺序 1–3 的五个变更）完成必须同时满足：

1. 未传 `plugins` 时，EnginePool 行为、工具注册表版本、默认存储、sink 对象身份和事件流与现在一致。
2. setup 失败不产出半初始化 pool；已成功 setup 的插件全部逆序回滚。
3. 每个注册都能通过 handle 撤销；两个 pool 之间没有 tool、hook、skill 目录或 sink 泄漏。
4. 插件生命周期事件经过 Console、JSONL 和 OTel；正文、凭据和业务字段不进事件。
5. 取消、close 超时、重复 close、依赖循环、重复 id、缺依赖、capability 冲突、skill id 重名均有测试。
6. 插件缺席时恢复会话的行为有测试固定。
7. `src/taifeng/plugin/` 不含业务术语；所有新增 / 修改文件 ≤ 800 行。
8. 全量模拟门禁与真实回归通过，`plugin_tool` 场景证明真实模型确实用到了插件注册的工具。

## 留给第二阶段的事

- 运行中卸载单个插件（需要 `ToolCallRuntime` 在途调用计数）。
- `engine` / `session` scope。
- 把 engine 事件流转发给插件 sink。
- 可选的插件自动发现。
