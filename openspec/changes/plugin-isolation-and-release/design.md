## Context

来源：计划 r2 的 Task 7 与 Task 9。分支 `feat/plugin-isolation-and-release`。这是插件第一阶段的收口变更。

## Goals / Non-Goals

目标是证明四个 capability 合在一起成立，并完成仓库要求的全部文档与真实回归义务。不引入新机制。

## Decisions

1. **隔离测试用两个真实 pool**。各加载一个同 id 的插件实例，分别断言 tool / hook / skill 目录 / sink 四类注册只在所属 pool 可见，关闭 A 不影响 B。不 mock 注册表。
2. **静态边界测试**。扫描 `src/taifeng/plugin/` 下所有 `.py`，拒绝 `tenant_id`、`user_id`、`os.getenv`、`os.environ`。
3. **示例只用通用名**。`ReadOnlyToolPlugin`（无副作用工具）、`JsonlAuditSinkPlugin`（把 pool 级事件写入构造时注入的路径）、`ExtraSkillsPlugin`（挂一个 skill 目录）。走 `SimClient`，无需 key。
4. **示例必须有带内成功标记**。仓库里多数 demo 末尾无条件 `return 0`，退出码不能说明跑通；本示例打印明确的成功 / 失败标记，并由 `scripts/verify_examples.py` 核验。
5. **真实场景断言分两路**。`plugin_tool` 场景里，pool 级 sink 上断言 `plugin_loaded` / `plugin_unloaded`，engine 事件流上断言 `tool_call_started` / `tool_call_completed`。两类事件走不同通道，不能混在一个流里断言。
6. **登记为实验能力**。`docs/capability-matrix.md` 标注实验层，直到第二阶段 scope 设计稳定。

## Risks

- 真实场景依赖模型选择调用插件工具；工具描述要足够直白，场景提示要明确要求调用。
- 文档同步面很宽（十个文件），容易漏；任务里逐个列出并各带验收。
