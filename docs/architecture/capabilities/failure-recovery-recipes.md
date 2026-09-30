# Capability: failure-recovery-recipes

> 状态：Experimental。关联 ADR 0017（规则③）/ 0033 / 0084。
> 实现：`src/taifeng/llm/recovery.py`（`RecoveryPlan` / `RecoveryRecipeBook` / 内核配方表）、
> `src/taifeng/loop/failure_policy.py`（`RecoveryRecipeProvider` / `RecipeDeclaringPolicy` / `resolve_recovery`）。
> 声明相关符号经 `taifeng.experimental` 导出；`RecoveryPlan` / `RecoveryStep` / `recommend_recovery` 属既有稳定面。

## Purpose

`turn_failed.data["recovery"]` 是机读的恢复配方：这类失败该按什么步骤处理、能不能自动重试一次、最后要不要升级
给人。内核自带一张按失败分类的配方表；本能力让业务**声明**自己的配方覆盖其中若干分类。内核只透出配方，
不执行——执行由业务编排层负责。

## 数据契约

### `RecoveryPlan`

| 字段 | 含义 |
| --- | --- |
| `failure_class` | 失败分类（`llm/errors.py::FailureClass`） |
| `steps` | 内核定义的恢复动作序列（`RecoveryStep`） |
| `auto_retry_once` | 是否建议自动重试恰好一次后再升级 |
| `escalate` | 自动手段耗尽后是否需要人工介入 |
| `custom_steps` | 业务声明的动作名（内核不解释）；默认空 |

`to_dict()`：`{failure_class, steps, auto_retry_once, escalate}`；`custom_steps` 非空时另带该键。

### `RecoveryRecipeBook`

不可变的配方表 = 内核配方 + 业务按分类声明的覆盖。

- `RecoveryRecipeBook.default()`：只含内核配方；
- `declare(*plans) -> RecoveryRecipeBook`：返回新表，原表不变；
- `declared(failure_class) -> RecoveryPlan | None`：业务声明的配方；
- `recommend(failure_class) -> RecoveryPlan`：声明优先，否则内核配方；未知分类按 `unknown` 的配方；
- `declared_classes`：声明过的分类集合。

### `RecoveryRecipeProvider`（Protocol）

失败处置 policy 的可选能力：`recovery_for(failure_class) -> RecoveryPlan | None`。同步纯函数。

`RecipeDeclaringPolicy(policy, recipes)`：给任一 `FailureDispositionPolicy` 加上这一能力；`decide` 原样委托被包装的
policy，`recovery_for` 返回配方表里声明的配方。

## Requirements

### Requirement: 声明在构造期校验

`declare` SHALL 在以下情形抛 `ValueError`，不产生新表：

| 情形 | 说明 |
| --- | --- |
| `failure_class` 不在内核定义的分类内 | 配方只能针对已知分类 |
| `steps` 为空 | 至少一个内核动作（不需要处理用 `RecoveryStep.NONE`） |
| `custom_steps` 含空白项 | — |
| `custom_steps` 的某项与 `RecoveryStep` 的取值同名 | 内核动作应写进 `steps` |
| 对 `cancelled` 声明重试（`auto_retry_once` 或 `steps` 含 `retry` / `backoff_retry`） | 取消是调用方的意图 |
| 同一次 `declare` 里同一分类出现多次 | — |

### Requirement: 失败事件透出声明的配方

`turn_failed.data["recovery"]` SHALL 由 `resolve_recovery(failure_policy, failure_class)` 给出（turn 内失败与
operation 级失败两条路径一致）：

- policy 未实现 `RecoveryRecipeProvider`、或 `recovery_for` 返回 None → 内核配方，形状与引入前逐键相同；
- 返回了配方 → 该配方的 `to_dict()` 外加 `source: "declared"`；
- 返回的配方 `failure_class` 与询问的分类不符 → 记 error 日志，使用内核配方；
- `recovery_for` 抛异常 → 记 error 日志（含堆栈），使用内核配方。

后两种情形退回内核配方是因为本函数运行在失败处置路径上，自身不能再抛；日志保证不静默。

#### Scenario: 为内容拦截声明转人工复核
- **GIVEN** `failure_policy = RecipeDeclaringPolicy(ConservativeFailurePolicy(), book)`，`book` 为 `content_filter`
  声明了 `steps=(adjust_input,)`、`custom_steps=("route_to_reviewer",)`
- **WHEN** 采样被内容安全拦截，turn 以 `turn_failed` 结束
- **THEN** `recovery == {failure_class: "content_filter", steps: ["adjust_input"], auto_retry_once: false,
  escalate: true, custom_steps: ["route_to_reviewer"], source: "declared"}`

#### Scenario: 未声明的分类
- **WHEN** 同一 policy 下发生 `runtime_io` 失败
- **THEN** `recovery` 为内核配方，不带 `source`

## R1–R5 影响

- R1：配方内容与自定义动作名由业务声明；内核不解释 `custom_steps`。
- R2 / R5：不涉及。
- R3：配方随 `turn_failed` 透出；provider 出错有 error 日志。
- R4：`recovery_for` 是同步纯函数，不得做 IO。
