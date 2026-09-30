# ADR 0081：压缩作为回访节点——可回到某次压缩之前

- 状态：Accepted（Amends #0014、#0016）
- 日期：2026-09-30
- 关联：[turn-rewind 契约 § 压缩是回访节点](../architecture/capabilities/turn-rewind.md)；
  [conversation 活文档 § 逻辑 history 重建](../architecture/conversation.md)；
  [context-compression 活文档](../architecture/context-compression.md)；ADR 0004 / 0014 / 0016 / 0059 / 0079 / 0080

## 背景

回访节点表只有采样与派发两类节点。压缩是对 history 影响最大的内核动作，却不可寻址：摘要一旦丢了关键信息
（漏掉某个约束、把用户原话转述走样），业务没有办法回到压缩之前，只能带着有损的上下文继续。

transcript 是 append-only 的：被折叠的原始条目物理上都还在，placeholder 记着替换区间。还原所需的信息一直存在，
缺的只是寻址与重放。

## 决策

1. **每个仍在逻辑 history 里的 `compacted` 条目是一个 `compaction` 节点**，`node_id = t{k}:cmp{m}`，
   `target_id` 为该条目的 id。`derive_rewind_log` 是唯一产出方——热路径在每次 turn 结束与 `CompactNow` 之后都从
   逻辑 history 重算节点表，不另做 live 记录。
2. **rewind 它 = 还原，不是截断**。还原后的 history 不是当前 history 的前缀，`history[:cut]` 表达不了。
   规划类型 `CompactionUndo` 与 `RetryCut` 同形，由 transcript 重放到该压缩条目之前得到。
3. **压缩动作写下的旁路项不属于「压缩之前」**。抢救摘要（`memory_pre_evict`）与钉回项（`pinned:*`）在 transcript 里
   落在 placeholder 之前，它们是这次压缩的产物。还原时去掉紧邻 placeholder 的这一段。周期性钉回项若恰好落在此处
   会被一并去掉——它是可重注入的状态，周期重注会在需要时补回。
4. **新增 `restore` 模式：只还原、不重推**。压缩可能发生在两轮之间，此时压缩之前的最后一条是模型的回答，
   重采样没有可回应的输入。而且还原通常伴随着「先改预算 / 换策略再继续」，下一步应由业务决定。
5. **`re_reason` 保留给轮到模型说话的情形**（还原后最后一条对话项是用户消息或工具结果），否则显式拒绝
   `nothing_to_redrive`。不静默退化成 `restore`——调用方要的是重推，得到一次不重推的成功会让它空等终结事件。
6. **marker 记 `undo_compaction` + `cut_index`**，冷重建按它还原；`cut_index` 是还原后长度，作为一致性校验。
   重放时为每次压缩保留「之前」的快照（引用，不复制条目）。
7. **仅 root thread**。spawn 子 thread 由后台驱动，句柄状态机没有「已还原、未运行」的状态；对其 compaction 节点
   提交 rewind 显式拒绝 `unsupported_node_kind`。

## 替代方案

- **把压缩之前的 history 另存一份快照到 store**：transcript 已含全部信息，再存一份是重复数据且要维护一致性；否决。
- **compaction 节点只支持 `re_reason`**：两轮之间的压缩就永远无法还原；否决。
- **还原后自动跳过下一次 pre-turn 压缩**：还原后的 history 可能确实超窗，跳过会直接溢出；是否再压、怎么压由
  当时生效的预算与策略决定；否决。
- **为就地改写 payload 的策略（surgical_trim / offload）也产节点**：它们不产生 `compacted` 条目，改写也不落
  transcript，没有可重放的依据；不在本决策范围。
- **指令热更等其他内核动作也作为节点**：它们在 history 中不留条目，无从推导；需要时应先让该动作落条目；
  不在本决策范围。

## 后果

- `RewindKind` 新增 `compaction`；`Rewind.mode` 新增 `restore`；`turn_rewound.data` 新增 `undo_compaction` /
  `redriven`；`rewind_rejected.reason` 新增 `nothing_to_redrive` / `unsupported_node_kind`。
- 节点表的消费方会看到新的节点类型；按 `kind` 分支渲染的 UI 需处理未知类型。
- `restore` 之后 history 变长，下一次 turn 可能立即再次触发压缩。
- R1–R5：R1 无业务概念；R2 `cache_anchor` 回退到替换区间起点之前，重推首采样的 cache 失效标 expected；
  R3 `turn_rewound` 带 `undo_compaction` / `redriven`；R4 还原是纯计算 + 一次 transcript 读取，重推沿用既有取消语义；
  R5 store append-only，冷重建与热内存一致。

## 验证

`tests/loop/test_rewind_compaction_node.py`：节点产出、按 turn 编号、不占用其他节点编号；还原结果、去掉旁路项、
保留普通注入、尊重更早的压缩、未知压缩报错；marker 冷重放、未知压缩与长度不符报错；`awaits_model` 各形态；
引擎级手动压缩进节点表、`restore` 还原（条目 id 一致、节点表重算）、还原后继续对话、冷加载一致、
两轮之间的压缩 `re_reason` 被拒且状态不动、mode 与节点类型不符被拒、采样前的压缩 `re_reason` 还原并重推。
