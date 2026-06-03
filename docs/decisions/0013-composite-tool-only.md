# ADR 0013: 放松 composite 校验 —— 允许 tool-only composite

- 状态：Accepted
- 日期：2026-06-04
- 关系：澄清 ADR 0006 本意（非推翻）

## 背景

ADR 0006 把 skill 分为 atomic / composite，atomic 禁止声明 `tool_names`，
故"只想调工具的叶子"必须升为 composite。而 `definition.py::validate()` 又
强制 composite 的 `child_skills` 非空，导致这类工具型叶子被迫凭空捏一个 dummy
子 skill 才能过校验（见 `tests/test_child_suspend_resume.py` 的 leaf-noop 占位）。

关键：ADR 0006 的数据结构只把 `child_skills` / `tool_names` 标为"composite 特有
字段（atomic 留空）"，**从未要求 composite 必须有非空 child_skills**。该约束是
`validate()` 后加的实现细节，并非决策本身。

## 决策

composite 的合法条件由"必须有 child_skills"放松为
**"`child_skills` 或 `tool_names` 至少其一非空"**。两者皆空 = 戴帽子的 atomic
（无意义空壳）→ 仍 fail-fast 拒绝。

`request_user_input` 维持普通工具语义，经 `tool_names` 显式授予，不引入任何内置
原语（保持"无配置即纯 LLM 调工具"范式）。

排除备选：
- 完全去掉非空要求（放进无子无工具空壳）—— 违背 fail-fast 调性。
- 改为允许 atomic 声明 tool_names —— 与"atomic = 纯内容、无 agency"定位相悖。

## 后果

- composite 语义从"有子 skill"修正为"有 agency（工具和/或子 skill）"，更贴合
  ADR 0006 本意。
- 工具型叶子（如分析 + `request_user_input` 采集）可写成 tool-only composite，
  不再需要 dummy 子 skill。
- atomic 约束不变（仍禁工具）。

## 相关

- ADR 0006（统一 skill 模型）
- 架构：`docs/architecture/skill-system.md`、`docs/architecture/capabilities/skill-dispatch.md`
