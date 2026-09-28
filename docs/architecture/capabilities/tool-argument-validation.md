# Capability: tool-argument-validation

## Purpose

工具参数在派发前按 `ToolSpec.input_schema` 校验；不合 schema 的调用**不执行 handler**，把具体违例
与期望 schema 作为错误结果还给模型，让它下一轮重写参数。

修复的缺口（2026-09-28 review）：派发层只检查「是不是合法 JSON 对象」，缺字段 / 类型错 / 枚举外
取值直接进 handler，靠各工具自己防御；`call_skill` 甚至把必填的 `reason` 以 `args.get` 兜底吞掉。

参照：opencode `tool/tool.ts` 的 `InvalidArgumentsError`。决策：ADR 0047。
实现：`tool/arg_validation.py`；接线 `loop/tool_batch.py`（主派发 / retry_tool / 编排）、
`loop/suspension_access.py` 与 `loop/child_resume_chain.py`（resume 执行路径）。

## 数据契约

| 符号 | 含义 |
| --- | --- |
| `schema_violations(schema, arguments) -> list[str]` | 确定违例（`$` 起的 JSON 路径 + 描述），至多 8 条；空 = 通过或无法判定 |
| `invalid_arguments_feedback(schema, violations) -> str` | `invalid_arguments: <违例>; ... The tool was not executed. Re-issue the call with arguments that match its input schema: <schema，≤2000 字符>` |
| `check_tool_arguments(schema, arguments) -> str \| None` | 上两者合一 |
| `arguments_rejection(registry, name, arguments, parse_error) -> str \| None` | 派发前参数关卡单一入口：JSON 解析错误优先，其次 schema 违例 |
| `dispatch_batch(..., registry=)` | 生产调用点必传；None 仅做解析校验（单测替身） |

### 覆盖的 JSON Schema 子集

`type`（含联合、`integer` 排除 bool）/ `required` / `properties`（递归）/ `enum` / `const` / `items` /
`additionalProperties: false`。其余关键字（`pattern` / `format` / `oneOf` / `$ref` / 数值范围等）
**放过不判**——宁可漏拦交给 handler，也不误拦合法调用。类型错时不再下探子结构（避免噪声）。

## 行为契约

### Requirement: 派发前拒绝，不执行 handler

顺序：`not_offered` → 参数（解析错误 → schema 违例）→ hook / 权限 / 锁 / handler。违例 SHALL 以
`ToolResult.error(反馈, reason="invalid_arguments")` 核销 call_id 并 emit `tool_call_completed`，turn 不中断。

#### Scenario: 模型改参重试
- **WHEN** 模型调用 `weather({"city": 42})`（schema 要求 string）
- **THEN** handler 不执行，输出含 `$.city: expected string, got integer` 与 schema
- **AND** 模型下一轮以 `{"city": "北京"}` 重调时正常执行一次

### Requirement: 全路径同一规则

turn 主派发、rewind `retry_tool` 补跑、声明式编排合成调用、resume 执行（permission allow /
`tool_outcome_unknown` retry）、子 thread 续跑 SHALL 共用 `arguments_rejection`。

## R1–R5 影响

R1：纯 schema 机制；R2：错误结果进 history 与普通工具结果同路；R3：复用 `tool_call_completed`；
R4 / R5：无影响。
