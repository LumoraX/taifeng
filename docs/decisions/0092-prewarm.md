# ADR 0092：预热——在用户输入到来之前做掉首轮采样的准备工作

- 状态：Accepted
- 日期：2026-09-30
- 关联：[prewarm 契约](../architecture/capabilities/prewarm.md)；
  [instructions-injection](../architecture/capabilities/instructions-injection.md)、
  [skill-working-set](../architecture/capabilities/skill-working-set.md)；ADR 0017 / 0090

## 背景

会话的第一次采样承担了全部的冷启动开销：动态指令层要去取，system prompt 与工具清单这段静态前缀
要被 provider 首次处理并写入 prompt cache。业务在用户打开对话框到敲下回车之间有空闲时间，
内核却没有入口让这段时间被用起来。ADR 0017 曾把预热判为「挂起等需求」；本轮由项目负责人明确要求实现。

命中 ADR 0017 规则①（内核机制缺口）；模型侧怎么预热属规则③（内核只定协议）。

## 决策

1. **预热是一个 Op（`Prewarm`），不是构造参数**。什么时候值得预热——建会话后立刻、空闲一段时间后、
   缓存将过期时——只有业务知道。
2. **三个步骤，可选可排序**：`instructions`、`working_set`、`model`。前两步没有外部成本，
   第三步可能花钱，业务可以只要前两步。
3. **模型侧预热是协议 `ModelPrewarmer`**，内核把「下一次采样会发出的请求」交给它。各家 provider 的
   缓存机制不同（显式断点、自动前缀缓存、连接预建），内核不假定其中任何一种。
4. **参考实现 `CachePrimingPrewarmer` 用一次输出极短的采样**。它对显式断点与自动前缀缓存都有效，
   代价是一次输入计费；是否值得由业务按计费与缓存时效判断，故不默认启用。
5. **预热请求与真实采样经同一套组装**。从采样准备阶段抽出无副作用的组装方法，预热与采样共用；
   各写一份的话，前缀迟早不一致，预热就白做了。
6. **预热持有 root gate，但给用户消息让路**。持有 gate 是为了读到一致的 history；用户消息开始排队之前
   先取消在飞的预热，真实的 turn 不等它。
7. **不留痕迹**。预热不是对话的一部分：不进 history、不占 turn 序号、不登记回访节点。
   rewind、压缩、审计都不需要知道它发生过。
8. **失败不传染**。预热是优化，不是前置条件；某一步失败记进事件后继续下一步，之后的 turn 照常进行。
9. **消耗记进会话账**。预热花的 token 是真实开销，受 `max_session_tokens` 约束；已触顶时跳过模型侧步骤。

## 替代方案

- **`EnginePool.get_or_create(prewarm=True)`**：建会话时自动预热。把时机定死在建会话那一刻，
  且建会话的调用要等预热，或者要引入后台任务的生命周期管理。Op 已经能表达这个用法。
- **让 `ModelClient` 协议多一个 `prewarm` 方法**：协议已有多个实现与包装层（重试、审计观测），
  每一层都要转发新方法，漏一层能力就静默消失。单独的协议由业务显式注入，不存在转发问题。
- **预热时直接跑一个不落盘的 turn**：turn 的副作用面很大（hook、工具、压缩、事件），
  要逐个关掉；组装请求是其中唯一需要的部分。
- **不持 gate，读 history 的快照**：预热与 turn 并行。预热请求可能基于过时的 history，
  且 turn 进行中时缓存本来就是热的，预热没有意义。
- **预热被取消时等它收尾再开始 turn**：违背让路的目的。取消是协作式的，
  预热器在下一个检查点退出。

## 后果

- 新增 Op `Prewarm`、事件 `prewarm_started` / `prewarm_completed`、构造参数 `model_prewarmer`。
- 新增实验层符号：`Prewarm`、`ModelPrewarmer`、`PrewarmOutcome`、`CachePrimingPrewarmer`。
- `loop/turn_sample.py` 的请求组装从采样准备阶段抽成独立方法，行为不变。
- 在飞操作的登记项多一个 `kind` 字段，用来识别预热。
- 审计模式下 `Prewarm` 不在允许的 Op 之列。
- 未覆盖：长期记忆的预取（查询取决于用户输入）；子 skill 与分离派发 child 的预热；
  探针在各家 provider 上是否真的让首轮命中缓存（需真实 LLM 验证）。
- R1–R5 见契约。

## 验证

`tests/loop/test_prewarm.py`：预热请求与首轮采样前缀相同、不留痕迹、消耗记进会话账、会话触顶跳过、
未注入预热器、预热器报告无事可做、失败不影响 turn、只做选定步骤、未知步骤拒绝、用户消息取消预热且
turn 不等它、`CancelTurn` 取消、排在运行中的 turn 之后、指令层提前解析且首轮命中缓存、
指令失败不阻塞后续步骤、工作集重算、探针保持前缀且不改原请求、探针可取消、参数校验、
用会话自己的客户端端到端。
