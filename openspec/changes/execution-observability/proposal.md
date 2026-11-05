## Why

多 Skill 并发已有事件，但同步子调用共享 submission_id，终端无法可靠区分交错输出。需要统一执行身份和可选状态投影，让宿主只接事件流即可直观看到调用情况。

## What Changes

- EventMsg 增加可选 execution 身份，增加采样边界 execution_updated 元数据事件。
- 提供 ExecutionProjection / ExecutionSnapshot 与 TerminalTreeSink 实验入口。
- 保留既有订阅、调度、存储与 ConsoleSink，补演示、边界测试和真实回归。

## Capabilities

### New Capabilities
- `execution-observability`：执行身份、阶段、不可变状态投影与终端调用树。

### Modified Capabilities
无；EventMsg 采用可选字段，既有事件保留语义。

## Non-goals

不实现网页、上下文分色、计划完成百分比、全局调度器、操作按钮或监控历史存储；不重建冷恢复前完整执行树。

## R1–R5 影响

R1：机制与显示分层，无业务名词。R2：纯元数据不进入prompt。R3：统一身份与采样更新。R4：任务本地作用域和独立有界sink，可取消。R5：不改 Journal，冷恢复重新观察，持久身份沿用thread。

## Impact

loop/event、turn、turn_sample、engine_events 与新增 execution_identity / execution_observer；新增 telemetry/execution_projection、terminal_tree；experimental 导出与 examples。同步 agent-loop、audit-observability、能力矩阵、usage、文档索引，新增 ADR 0132。
