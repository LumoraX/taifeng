# ADR 0083：压缩增量基线——上次压缩后没长多少就不再压

- 状态：Accepted
- 日期：2026-09-30
- 关联：[compaction-growth-baseline 契约](../architecture/capabilities/compaction-growth-baseline.md)；
  [context-compression 活文档](../architecture/context-compression.md)；ADR 0004 / 0017 / 0036 / 0043 / 0059

## 背景

压缩触发是绝对阈值：估算 ≥ 软阈值就压。当压缩腾不出多少空间时——保留的尾部本身很大、ADR 0059 原样保留的用户
原话很长、钉回的状态很多——压缩之后估算仍在软阈值之上。于是下一轮的 pre-turn 检查、工具循环里每一圈的 mid-turn
检查都会再压一次：

- 每次 pre-turn 压缩都改写 head，provider 缓存全部失效；
- handoff 每次都要调一次模型，对上一次的摘要再做摘要，信息逐次流失（G1c 的降级告警只是提示，不阻止）；
- 腾出的空间趋近于零。

第三轮对比分析把它列为 A5「压缩相对增量基线」，命中 ADR 0017 规则①。

## 决策

1. **基线 = 压缩应用完成时的上下文估算**，下一次自动压缩要等估算达到 `基线 + ceil(基线 × ratio)`。
2. **基线记在 `compacted` 条目的 metadata 里，随条目落 transcript**。不在 engine / runner 上新增跨 turn 状态：
   - 冷加载后无需任何恢复逻辑，闸门状态自然一致（R5）；
   - 不必穿透 pool → engine → runner 的构造链（`engine.py` 已接近行数上限）；
   - rewind 回到压缩之前时基线随条目一起离开逻辑 history，不会残留。
3. **到硬阈值不设闸**。闸门是为了避免无效压缩，不是为了冒溢出的风险。
4. **`CompactNow` 与 overflow 自愈不设闸**：前者是业务的显式意图，后者是 provider 已经判了超长。
5. **无论闸门是否打开都记录基线**：开关只影响判定，不影响数据。
6. **默认关闭**（`recompact_min_growth_ratio = 0`），可经 `UpdateBudget` 运行时调整。
7. **推迟时发 `compaction_deferred`**，与 `pre_compact_hook_skipped` 同为「本次没压」的可观测信号。

## 替代方案

- **按压缩次数设冷却**（压缩后 N 轮内不再压）：与上下文实际增长脱钩，N 轮里可能已经涨到硬阈值边缘，也可能
  一点没涨；否决。
- **压缩后把软阈值抬到基线之上**：等价于改预算，业务配置的阈值被内核悄悄改写；否决。
- **在 runner / engine 上持有基线**：见决策 2；否决。
- **压缩收益过低时判压缩失败并回滚**：压缩已经调过模型、花了钱，回滚不能把它要回来；而且收益低不等于不该压
  （到硬阈值时再少也得压）；否决。

## 后果

- `ContextBudget` 新增 `recompact_min_growth_ratio`；`UpdateBudget` 新增同名字段；新增事件 `compaction_deferred`。
- `compacted` 条目的 metadata 多一个键 `post_compaction_tokens`。旧 transcript 的压缩条目没有该键，对它们不设闸。
- 打开闸门后，上下文会在「软阈值之上、硬阈值之下」停留更久；`output_reserve_tokens` 与硬阈值的配置需留出余量。
- 就地改写类策略（`surgical_trim` / `offload` / `multimodal_evict`）不产生条目、不记基线，也不受闸门针对性约束——
  但闸门在策略选择之前判定，推迟时它们同样不会运行。
- R1–R5 见契约。

## 验证

`tests/loop/test_compaction_growth_baseline.py`：基线读取（取最后一次压缩、无压缩、无记录、畸形值）、闸门默认关闭、
增长不足拦截与边界值、硬阈值优先、无基线不设闸、非法比例拒绝；TurnRunner 级基线落在 history 与 store 的同一条目上、
闸门关闭时也记录、无闸门时每轮都压、有闸门时推迟并上报事件内容、增长足够后放行、强制压缩不受约束；
`UpdateBudget` 运行时调整与非法值保持原值。
