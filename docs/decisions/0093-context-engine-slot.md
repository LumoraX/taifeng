# ADR 0093：ContextEngine 可插拔槽位——history 不动，只改发出去的视图

- 状态：Accepted
- 日期：2026-09-30
- 关联：[context-engine 契约](../architecture/capabilities/context-engine.md)；
  [cache-anchor](../architecture/capabilities/cache-anchor.md)、
  [token-accounting-calibration](../architecture/capabilities/token-accounting-calibration.md)；
  ADR 0017 / 0087 / 0092

## 背景

内核把完整的逻辑 history 发给模型，超预算时由压缩策略改写 history。压缩策略已经可插拔，但它只有
一种作用方式：破坏性地改写。想做「只发最近几轮，但旧内容留着以后检索」「按相关性挑历史片段」
这类非破坏性的上下文管理，业务没有入口——发给模型的内容恒等于 history。ADR 0017 曾把
ContextEngine 槽位判为「挂起等需求」；本轮由项目负责人明确要求实现。

命中 ADR 0017 规则①（内核机制缺口：装配是采样路径的一环）；取舍口径属规则③（内核只定协议）。

## 决策

1. **槽位的职责是装配视图，不是接管压缩**。压缩归 `CompressionStrategy`，它已经是可插拔的；
   在引擎协议里再开一个压缩入口，两套入口的优先关系就得另行约定。
2. **视图与 history 分离**。history 是持久化的事实，视图是某一次采样的输入。视图不落盘、
   不进回访节点、不参与 rewind；冷恢复后由引擎对同一 history 重新装配。
3. **视图可以包含 history 里没有的条目**。只允许取子集的话，检索召回、业务生成的摘要这类
   用法就得先把内容写进 history，而那正是要避免的。
4. **内核只做结构校验**：工具调用与结果成对、非空、锚点不越界。内容取舍对不对，内核无从判断。
5. **不合法的视图使 turn 失败，不退回完整 history**。退回看起来更稳，但引擎通常是因为完整 history
   放不进窗口才存在的，静默退回会把问题变成一次难以归因的超限错误（仓库的「禁止静默降级」）。
6. **预算与压缩触发按视图估算**。按 history 估算的话，引擎把视图压得再小，history 一长压缩照样
   触发，把引擎想留着的内容折叠掉。
7. **同一 history 版本只装配一次并缓存**。预算判定与采样之间不能各装配一次——两次结果不同时，
   判定通过的与实际发出的就不是同一份。
8. **发出的是视图时不校准 history 的 token 估算**。provider 回报的实测值对应视图，
   拿它校准 history 前缀会让之后的估算整体偏移。
9. **缓存影响由引擎声明**（`cache_invalidated`、`anchor_preserved_until`），与压缩结果的声明方式一致。
   内核据此放缓存断点，并把随后的 cache 失效归因为 `context_engine`。
10. **引擎挂在 `CompressionOrchestrator` 上**。协调器已经贯穿每一个 runner 的构造点
    （根 turn、子 skill、分离派发、恢复链、手动压缩），且同属上下文管理；不为它另开一条参数链。
11. **`after_turn` 尽力而为**，与长期记忆的写回一致：建索引失败不应让一轮已完成的对话变成失败。

## 替代方案

- **照搬 openclaw 的完整生命周期（bootstrap / ingest / assemble / compact / afterTurn）**：
  其中 compact 与既有的压缩槽位重复，bootstrap / ingest 在这里没有对应的时机——history 的装载与
  追加由 store 与 runner 负责。只取有缺口的两项。
- **把视图做成一种压缩策略**（返回改写后的 history 但不落盘）：压缩结果会成为新的 history，
  落盘、进回访节点、参与 rewind；「不落盘的压缩」要在这些位置逐个加例外。
- **视图只能是 history 的子序列**：见决策 3。
- **每次采样都重新装配**：引擎若带检索，一轮里多次采样会重复检索；且与决策 7 冲突。
- **在 `EnginePool.create` 到 `TurnRunner` 之间加一条 `context_engine` 参数链**：要改的构造点有六处，
  其中两个文件已在行数上限附近。

## 后果

- 新增实验层符号：`ContextEngine`、`AssembleRequest`、`AssembledContext`、`TurnUpdate`、
  `ContextEngineError`、`TailWindowContextEngine`。
- 新增事件 `context_assembled`；构造参数 `context_engine`；`CompressionOrchestrator` 多一个关键字参数。
- 注入了引擎而没有压缩策略时也会构造协调器；压缩入口对「没有任何策略的协调器」直接返回，
  不发起压缩尝试。
- 采样准备阶段的请求组装方法变为异步（ADR 0092 抽出的那一个）。
- 审计模式不支持，注入时构造期拒绝（`audit_context_engine_unsupported`）。
- 未覆盖：视图语义的校验；Responses 协议下推理项与调用搭配关系的专门校验（沿用请求组装阶段的
  既有校验）；引擎自身状态的持久化（由引擎负责）。
- R1–R5 见契约。

## 验证

`tests/context/test_context_engine.py`：悬空检测、合法视图、含 history 外条目、空视图、拆散工具对、
history 里本来悬空的调用、锚点越界、窗口未满原样发送、开头加最近几轮、保留首条用户消息之前的条目、
窗口移动才破坏缓存、按 thread 分别记、取消、参数校验。
`tests/loop/test_context_engine_slot.py`：发送视图而 history 与落盘完整、原样发送、视图含不进 history 的
条目、同一版本只装配一次、不合法视图使 turn 失败且 history 不受影响、引擎异常、轮后通知的内容、
轮后通知失败被忽略、子 skill 单独装配、视图在预算内时不触发压缩、未启用时无协调器、审计模式拒绝。
