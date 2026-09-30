# ADR 0087：后台延迟压缩——先在后台算摘要，下一轮开始时应用

- 状态：Accepted
- 日期：2026-09-30
- 关联：[compaction-background 契约](../architecture/capabilities/compaction-background.md)；
  [context-compression 活文档](../architecture/context-compression.md)；ADR 0004 / 0029 / 0036 / 0059 / 0083

## 背景

handoff 压缩要调一次模型生成摘要。它发生在 pre-turn 检查里——用户提交消息之后、模型开始回答之前。用户感受到的是
「这一条消息的首字延迟多了十几秒」，而且恰好发生在长会话里，最影响体验的时候。

压缩并不需要在那个时刻计算。上下文越过软阈值到逼近硬阈值之间通常还有好几轮对话的余量，摘要可以提前算好。
能力侧 review 把它列为候选。命中 ADR 0017 规则①（内核机制：上下文管理）。

## 决策

1. **做成包装策略，延迟逻辑完全落在 `CompressionStrategy` 协议之内**。不在主循环里加调度器、不新增 Op、
   不穿透构造链。`should_trigger` 返回 None 即「这次不压」，正好表达「后台在算，本轮先走」。
2. **在快照上算，应用时核对前缀**。history 只追加：计算期间新增的条目都在快照之后，结果可以直接接上它们。
   rewind、rollback、另一次压缩会改写前缀，此时逐条目 id 比对失败，结果作废。
3. **只在 pre_turn 起后台、只在 pre_turn 应用**。后台结果按 pre_turn 语义算出（可以动 head）；mid_turn
   应用它会改写已缓存的前缀，违反 R2。mid_turn 只在逼近硬阈值时交给内层按 mid_turn 语义同步处理。
4. **`urgent_ratio` 之上同步压缩**：后台没算完而上下文已经逼近硬阈值时，等待比阻塞更糟——下一次请求可能直接溢出。
5. **后台失败过一次就退回同步**，不在后台反复重试。同步路径会把失败如实返回，由既有的失败处理接手。
6. **必须是 `compressors` 里唯一的策略**，兜底策略放进内层。外层 orchestrator 是 first-wins 且在策略不触发时继续
   往下找；并列的兜底策略会在后台计算期间立刻执行，延迟就失去意义。
7. **pool 关闭时统一收尾**：`close_engine_pool` 对每个提供 `aclose` 的压缩策略调用它。以鸭子类型识别，
   不扩 `CompressionStrategy` 协议——绝大多数策略没有需要关闭的东西。
8. **后台任务的开始与结束不单独发事件**。事件必须归属某个 submission，而后台任务跨 turn 存活；
   应用时的 `compaction_completed.detail.background` 已说明这次压缩来自后台。

## 替代方案

- **turn 结束后由 engine 自动提交一次 `CompactNow`**：压缩仍在 actor 里串行执行，下一条用户消息照样要等它跑完，
  只是等待提前开始；而且手动压缩不受增量基线约束；否决。
- **在 engine 里加后台压缩调度器**：要新增跨 turn 状态、穿透构造链（`engine.py` 已接近行数上限），
  并在主循环里处理应用时机；包装策略用既有的检查点就能表达；否决。
- **后台结果在 mid_turn 也应用**：见决策 3；否决。
- **前缀不匹配时尝试修补**（重算被改动的部分）：rewind 之后的 history 与快照的关系不可一般化；作废重算
  是唯一不会出错的做法；否决。

## 后果

- 配置了本策略后，压缩比不配置时晚一轮生效；软阈值应相应调低，给后台留出时间。
- 后台任务与当前 turn 并发调用模型：摘要请求与对话请求同时在飞，provider 侧的并发与限流由业务的 client 负责。
- 未应用的后台结果只在内存，进程退出即丢失。
- `EnginePool.close()` 多一步策略收尾。
- R1–R5 见契约。

## 验证

`tests/context/test_background_compaction.py`：协议符合；首次检查起后台且不阻塞；算好的结果在下一次 pre_turn
应用、接上新增条目、不再调用内层；mid_turn 不应用；前缀改写后作废重算；逼近硬阈值同步压缩；mid_turn 不起后台；
后台返回失败与抛异常后退回同步并留日志；每 thread 至多一个任务；thread 隔离；内层不触发时无动作；空 history；
`aclose` 取消并退回同步；强制压缩走同步；参数校验；引擎级端到端（轮次不被阻塞、下一轮应用、条目落 transcript
并带增量基线、pool 关闭后无残留任务）。
