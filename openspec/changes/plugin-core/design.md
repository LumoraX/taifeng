## Context

来源：`docs/superpowers/plans/2026-11-15-taifeng-plugin-system.md`（r2）的 Task 1–3。在独立 worktree、分支 `feat/plugin-core` 实现。它是其余五个插件变更的前置。

## Goals / Non-Goals

目标是一个可加载、可回滚、可观察的 pool 级插件核心，未传 `plugins` 时行为与现在逐字节一致。不在本变更里提供任何具体 capability。

## Decisions

1. **显式注入**。只接受 `EnginePool.create(plugins=...)`，默认空元组；不扫描 entry points。
2. **一套清理路径**。`Plugin.setup(ctx) -> None` 不返回值；context 把每个 `RegistrationHandle` 自动登记到 manager，manager 负责逆序关闭。`Plugin.close()` 可选，用 `getattr` 探测，先于该插件的 handle 关闭。
3. **handle 统一异步**。`RegistrationHandle` 只有幂等的 `async aclose()`，因为 skill 目录撤销要重新 discover。
4. **类型**。`PluginSpec` 为 `@dataclass(frozen=True, slots=True)`：`id`、`version`、`requires: tuple[str, ...] = ()`、`scope: Literal["pool"] = "pool"`。
5. **加载**。按 id 建表 → Kahn 排序（同层按传入顺序）→ 逐个 setup。重复 id、缺依赖、有环在任何 setup 运行前抛错。setup 抛错时先撤销该插件自己的 handle，再逆序关闭已成功的插件；原始错误为首因，回滚错误只计数。
6. **封口**。setup 返回后该插件的 context 立即封口，再注册抛 `PluginCapabilityError`。
7. **状态机**。`new → loading → loaded → closing → closed | closed_with_errors`，`snapshot()` 返回冻结的 `PluginSnapshot(state, loaded_ids)`。
8. **事件**。三个事件类放 `loop/event_lifecycle.py`（`event_turn.py` 已 717 行），kind 加入 `event_base.py` 的 `MsgKind`，类加入 `event.py` 的 union。字段固定：`plugin_id`、`version`、`scope`、`duration_ms`、`failure_class`、`rollback_errors`。发送沿用 `loop/pool_session.py` 的先例：`EventMsg(submission_id="*", msg=...)` 直接交给 pool 级 sink，`seq=0`；无 sink 时只记日志。
9. **加载点**。`cls(...)` 构造出 pool 之后、`create` 返回之前。此时工具注册表、skill 注册表、`_root_cancel` 都已就绪，尚无 engine。失败走 `create` 既有的失败清理路径。
10. **关闭点**。`pool_lifecycle._cleanup_pool_resources` 最前面（停 watcher 之前），沿用「保留首错、不阻断后续清理」。此时工具集事件已退订、Session 已全部强制释放，所以不存在在途调用，也不需要 `PluginActiveRegistrationError`。
11. **行数**。`loop/pool.py` 现 784 行，只允许加参数和一次调用；其余全部进 `loop/pool_plugins.py`。超线就先把一段既有逻辑搬出去。

## Risks

- `create` 的失败清理路径分两段 `try`，加载点落在第二段；要确认 pool 已构造时的清理不会重复关闭 store。
- 事件无 engine 可发，`seq=0`；订阅方若按 seq 做连续性自检需排除 `submission_id="*"` 的 pool 级事件（现有 pool 级事件已是如此）。
