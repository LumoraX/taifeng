## Why

前四个变更各自交付一块机制，但「两个 pool 互不泄漏」「示例能跑」「文档与能力矩阵已登记」「真实模型确实用到了插件工具」这些要在全部到位后统一验收，否则插件能力不能对外声明。

## What Changes

- 跨 pool 隔离测试与 `src/taifeng/plugin/` 的静态边界测试。
- 无业务名的可运行示例 `examples/plugin_system/demo.py`。
- 同步全部活文档与能力登记。
- 真实回归新增 `plugin_tool` 场景并提交台账。

## Capabilities

### New Capabilities

- `plugin-isolation-and-release`：插件能力的隔离保证与对外声明条件。

### Modified Capabilities

无。

## Non-goals

不新增内核机制；不把插件 API 晋升到稳定层（保持在 `taifeng.experimental`）；不做发版动作。

## R1–R5 影响

- R1：本变更用静态测试把 R1 固化为门禁。
- R2–R5：无新增机制，只验证前四个变更的声明成立。

## Impact

实现：`examples/plugin_system/demo.py`、`examples/real_llm/capability_matrix.py`。

测试：`tests/plugin/{test_scope,test_static_boundary}.py`、`tests/examples/test_plugin_system.py`。

文档：`docs/architecture/{overview,agent-loop,skill-system,public-api}.md`、`docs/architecture/capabilities/{plugin-system,README}.md`、`docs/configurable-knobs.md`、`docs/capability-matrix.md`、`docs/README.md`、`docs/real-llm-ledger.{json,md}`（生成）。

前置：`plugin-core`、`plugin-tool-hook-registration`、`skill-dir-registration`、`telemetry-fanout-sink` 全部合入。
