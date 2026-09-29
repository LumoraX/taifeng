# ADR 0084：失败恢复配方可由业务声明

- 状态：Accepted
- 日期：2026-09-30
- 关联：[failure-recovery-recipes 契约](../architecture/capabilities/failure-recovery-recipes.md)；
  [llm-client 活文档 § 错误分类与恢复](../architecture/llm-client.md)；ADR 0017 / 0033 / 0037

## 背景

`turn_failed.data["recovery"]` 来自内核里一张写死的表。表的内容是合理的默认值，但「某类失败该怎么办」在不同部署里
答案不同：有的部署对限流要直接升级（配额是硬约束），有的对内容拦截要转人工复核，有的对 provider 5xx 要换端点。
业务现在只能忽略事件里的配方、在自己的编排层另查一张表——两张表各说各话，事件里的配方成了摆设。

能力侧 review 把它列为「只定协议」的候选。命中 ADR 0017 规则③（内核定协议，内容由业务提供）。

## 决策

1. **配方表 `RecoveryRecipeBook`**：内核配方之上按失败分类覆盖，不可变，声明在构造期校验。
2. **经失败处置 policy 注入**，作为其可选能力 `RecoveryRecipeProvider`。不新增 `EnginePool` 参数：
   policy 已经穿透到 turn 内失败与 operation 级失败两个调用点；恢复配方与处置裁决本就是同一件事的两面
   （挂不挂、之后怎么办）。
3. **`custom_steps` 承载业务特有的动作**，内核不解释。内核动作仍是封闭枚举——它们是内核自己能理解的语义
   （压缩、退避），业务动作不应混进去；反过来业务动作也不得与内核动作同名。
4. **声明的配方在事件里带 `source: "declared"`**；内核配方的形状逐键不变，既有消费方不受影响。
5. **失败处置路径上不抛异常**：provider 出错或返回了张冠李戴的配方时记 error 日志并退回内核配方。
   这不是静默回退——有日志、事件里没有 `source: "declared"`，两处都能看出声明没有生效。
6. **不允许对 `cancelled` 声明重试**：取消是调用方的意图，重试它等于违背取消。
7. **内核仍然只透出、不执行**（ADR 0017）。自动重试由 `RetryingModelClient` 与挂起 / Resume 机制承担，
   配方是给业务编排层与 UI 的机读建议。

## 替代方案

- **`EnginePool.create(recovery_recipes=)`**：需要穿透 pool → engine → runner，而 policy 已在两端；否决。
- **允许业务新增失败分类**：分类由内核的异常归类产生，业务新增的分类永远不会被命中；否决。
- **让内核按配方自动执行**：自动重试的语义（零产出才重试、审计模式互斥）已由 ADR 0037 定义，配方层再执行一遍
  会产生两套重试；否决。
- **provider 出错时让 turn 以另一种失败结束**：把一次已经发生的失败变成两次；否决。

## 后果

- `RecoveryPlan` 新增 `custom_steps`（默认空，`to_dict` 在为空时不带该键）。
- 新增实验层符号 `RecoveryRecipeBook` / `RecipeDeclaringPolicy` / `RecoveryRecipeProvider`。
- `llm/recovery.py` 在运行期导入 `FailureClass`（此前仅类型检查期）；无循环依赖。
- R1–R5 见契约。

## 验证

`tests/llm/test_recovery_recipes.py`：默认表与内核表一致、声明只覆盖本分类且不改原表、未知分类回落、
`custom_steps` 序列化、五类非法声明、重复声明；包装 policy 保持原裁决、能力识别、无 provider 时用内核表、
声明的配方带 `source`、张冠李戴与抛异常时退回内核配方并留日志；引擎级 `turn_failed` 带声明的配方。
既有 `tests/llm/test_recovery.py` / `tests/test_failure_policy.py` 未改动即通过。
