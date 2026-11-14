# ADR 0143：执行身份晋升稳定层

- 状态：Accepted
- 日期：2026-11-14
- 关联：[execution-observability 能力契约](../architecture/capabilities/execution-observability.md)、[ADR 0066 公共 API 分层](0066-public-api-tiers-and-deprecation.md)、[ADR 0141](0141-observability-resource-governance.md)

## 背景

`EventMsg` 在稳定层，它的 `execution` 字段的类型 `ExecutionIdentity` 却只从 `taifeng.experimental` 导出，违反「稳定协议里出现的类必须在公共 API」的规则。宿主（xuanpu R5）要按租户持久化执行树与 span，只依赖稳定层，无法合规地读取这个字段。

执行身份、`execution_updated` 的字段与用量计数已有引擎级测试，并在 2026-11-06 的 codex / gpt-5.6-luna 全量真实矩阵中通过（36/36）。

## 决策

1. `ExecutionIdentity` 晋升到 `taifeng.__all__`；`taifeng.experimental` 经 `__getattr__` 保留同名入口至少一个发布版本，访问时发 `DeprecationWarning`。
2. 以下内容成为稳定契约，只能向后兼容地增加字段：
   - `ExecutionIdentity` 的七个字段及冻结语义；
   - `EventMsg.execution` 的可空语义（旧事件、预热或裸采样为 `None`）；
   - `execution_updated` 的数据键：`phase`、`iteration`、`usage`、`usage_complete`、`context_tokens`、`context_window`、`context_measured`、`usage_attempts_started`、`usage_attempts_metered`、`usage_attempts_missing`，以及 `started = metered + missing`。
3. 投影与显示器（`ExecutionProjection`、`TraceProjection`、`TerminalTreeSink`、`trace_history` 的树与瀑布）**保持实验层**。宿主若要持久化，应基于稳定事件自建，不依赖进程内投影。

## 后果

- 宿主可只用稳定层读取父子关系、尝试与用量；冷恢复不重建父级的限制不变（R5 不保证）。
- `tests/test_execution_identity_stable.py` 守护字段集合、事件信封类型与数据键；改名或删除需要走弃用流程。
- 本变更只改导出位置与文档，不改 `loop/` 运行行为，不触发真实回归红线；台账沿用 2026-11-06 记录。
