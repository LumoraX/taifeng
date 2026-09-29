# ADR 0079：并行批次里的 retry_tool 按批次截断，只重跑目标调用

- 状态：Accepted（Amends #0014、#0016）
- 日期：2026-09-30
- 关联：[turn-rewind 契约 § retry_tool](../architecture/capabilities/turn-rewind.md)；
  [conversation 活文档 § 逻辑 history 重建](../architecture/conversation.md)；ADR 0014 / 0016 / 0018

## 背景

ADR 0014 的 `retry_tool` 截到 `inner_history_len`（调用之后、结果之前）再补跑。这个截点隐含「一次采样只有一个调用」。
契约把并行批次列为 v1 不支持。在 main（c3aa46b）核对一次采样发出 `a`、`b`、`c` 三个调用后对 `b` 做 retry_tool 的结果：

- **Chat 布局**（逐对交错）：截点在 `b` 的调用之后，`c` 的调用与结果被一并截掉。history 自洽，但 `c` 的工作凭空消失，
  模型续推时可能重做、也可能漏掉。
- **Responses 布局**（调用成组在前）：热路径记录的截点在 `b` 的结果之前，`c` 的结果被截掉而调用还在，留下未配对的调用；
  冷推导的截点（调用下标 + 1）落在调用组中间，热冷坐标不一致。

`max_parallel_tool_calls` 只约束执行并发度，模型一次采样发出多个调用与它无关——默认配置下同样会遇到。

## 决策

1. **截断按批次规划**。批次 = 目标调用所在的、由意图 / 调用 / 结果组成的连续段，遇到其他类型的条目或另一次采样的
   调用 / 结果即止。规划产出 `RetryCut{cut_index, drop_index?}`：保留到批次末尾，只去掉目标调用的旧结果。
2. **规划只看 history 内容，不依赖 `inner_history_len` 的具体取值**。该字段在两种布局、热冷两条路径下含义不完全一致；
   规划只用它做「节点所在位置」的上界，用来在 call id 被复用时定位是哪一次调用。
3. **单调用批次的结果与原行为逐位相同**：旧结果是批次最后一条时 `cut_index` 就是它的下标，不带 `drop_index`。
   既有 marker、事件与测试不受影响。
4. **marker 记录 `drop_index`，冷重建按同一规划重放**。store 仍然 append-only：被去掉的旧结果物理保留，
   逻辑 history 里不再出现。`drop_index` 越界或指向的不是结果条目时抛 `ValueError`，不猜。
5. **根路径与 spawn 子 thread 路径共用同一个纯函数**，`new_args` 的改写也合并为一个实现；改写只换 `arguments`，
   保留条目 id 与采样归属。
6. **补跑出的结果带上采样归属**（Responses 路径）。此前 `complete_seed_call` 不带，属同一类配对缺口，一并修正。
7. **批次之后的内容丢弃**：包括后续采样圈与其间的注入项。它们都建立在旧结果之上。

## 替代方案

- **整批重跑**：实现最简单，但会重复同批其他调用的副作用，违背 retry_tool「只重跑该工具」的语义；否决。
- **新结果原位替换旧结果**（保持结果顺序）：store 是 append-only，新结果只能追加在 marker 之后；原位替换需要
  marker 携带完整新条目，冷重建逻辑更复杂。配对按 call id 而非位置，结果在批次内的先后对 provider 无影响；否决。
- **把并行批次的 retry_tool 拒绝掉**（显式 `rewind_rejected`）：比静默丢数据好，但能力缺口仍在；否决。
- **在 `RewindCheckpoint` 上新增批次边界字段**：热路径记录时批次尚未结束（逐调用记录），冷推导也要回看；
  而规划函数直接看 history 就能确定边界，无需新字段；否决。

## 后果

- `turn_rewound.data` 新增 `drop_index`（无则为 null）；rewind marker 的 payload 可能带 `drop_index`。
  旧 transcript 没有该键，冷重建行为不变。
- 契约的 v1 边界去掉「`retry_tool` 假定串行派发」。
- 节点与 history 对不上时 retry_tool 由此前的「按下标硬截」变为显式 `rewind_rejected(unknown_node)`。
- R1–R5：R1 无业务概念；R2 `cache_anchor` 回退到第一个变化下标之前，重推首采样的 cache 失效仍标 expected；
  R3 `turn_rewound` 带完整坐标；R4 补跑沿用既有取消语义；R5 store append-only 不变，冷重建与热内存一致。

## 验证

`tests/loop/test_rewind_parallel_batch.py`：规划函数在 Chat / Responses 两种布局、首 / 中 / 末调用、批次后有注入项、
目标无结果、call id 复用、非 dispatch 节点、节点与 history 不一致下的结果；marker 坐标与冷重建重放、越界与类型校验；
引擎级只重跑目标调用（执行次数、条目 id 保留、无未配对调用、续推请求里三个结果齐全、sim 无合同违规）、
`new_args` 只改目标调用、冷加载与热内存逐项相同、同一批次连续两次 retry。
既有 `tests/loop/test_turn_rewind.py` / `test_rewind_cold.py` / `test_rewind_thread.py` 未改动即通过。
