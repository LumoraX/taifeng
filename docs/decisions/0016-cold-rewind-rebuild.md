# ADR 0016: turn-rewind 冷场景重建 —— 读时重建逻辑 history + 从 history 推导节点表

- 状态：Accepted
- 日期：2026-06-07
- 关系：**Supersedes #0014** 的 node_id 格式段（`it{n}` → `t{k}:it{n}`）；扩展 ADR 0014（不推翻截点 / mode 语义）；与 ADR 0008（store 协议解耦 / JSONL append-only）、ADR 0012（suspend-resume 原语）协作

## 背景

ADR 0014 的 rewind 节点表（`RewindLog`）**只活在内存**，只在热 turn 跑完与 `CompactNow` 回写 engine。没有任何 load / resume 路径重建它：engine `__init__` 明确不调 `store.load_thread`。

→ 冷 worker / 跨进程时，即便 history 经 `initial_history` 灌回内存，`rewind_nodes()` 仍空 → 任何 `Rewind` 命中 `unknown_node` 拒绝。业务侧"从某一步重跑"在冷场景完全不可用。

更深的根因：**持久化 transcript ≠ 热内存 history**。append-only 主存在两种情况下与内存发散：

1. **压缩**：内置策略把内存 history 替换成 `[head, placeholder, tail]`，但只把 placeholder **append 到 JSONL 末尾**，被替换区间物理留存（R5 append-only）。直接读 raw 得到废弃中间项在前、placeholder 在末尾——与内存结构性发散，下标不对齐。
2. **历史 rewind / rollback**：截断只动内存，store 保留完整留痕（被截项 + marker + re-run 项）。直接读 raw 包含废弃尾部，与内存发散。

这两种发散同时暴露了一个**既存隐患**：现有 resume 路径把含废弃项的 raw history 直接喂 LLM——resume 一个压缩过的 thread 会把被压缩内容重发一遍（不崩、故未被发现）。rewind 对下标敏感，废弃段产节点、`history_len` 落死区、re-run 撞号——是正确性 bug。

## 决策

### 决策一：读时重建逻辑 history，不改写时存储布局

新增 `reconstruct_logical_history(raw)` 纯函数（`conversation/reconstruct.py`），把 append-only transcript 顺序重放成与热内存等价的逻辑 history。

**为什么不改写时布局**：

- R5 append-only 是核心约束（进程崩溃可 resume、不丢留痕）；改写时意味着压缩时要"覆盖"或"删除"旧项，破坏 append-only。
- 写路径改动风险远大于读路径：压缩、rewind、resume 多个写点需同时改，且业务侧自实现的 `MessageStore` 无法强制约束。
- 读时重建纯 CPU、无 IO、对干净 thread 是恒等映射（向后兼容），可单独测试，且**顺带修正** resume 废弃项重放隐患（副作用纯正）。

重建规则：扫一遍 raw，遇 `compacted`（含 `replaced_range`）折叠废弃区间、把 placeholder 移到正确位置；遇 `rewind` / `rollback` marker（payload 含 `cut_index`）截断 logical 到 `cut_index`，marker 本身不入 logical（与热路径一致）；其余 item 顺序追加。多次压缩按写入序天然顺序折叠，无特殊处理。

**已知边界**：旧版本（ADR 0014 之前、无 `cut_index`）写出的 rewound transcript，`reconstruct` 遇缺 `cut_index` 的 marker 显式报错（不静默猜下标），作为已知边界文档化——冷 rewind 是新能力，不存在"需要冷 rewind 的历史遗留 transcript"。

### 决策二：从逻辑 history 推导节点表，不持久化 checkpoint

新增 `derive_rewind_log(history)` 纯函数（`loop/rewind.py`），在重建后的逻辑 history 上推导全 turn 节点表。

**为什么不持久化 checkpoint**：

- 持久化 checkpoint 依赖"写时坐标系"——但 transcript 含 marker / 废弃项，写时下标与逻辑 history 下标不对齐（正是 §背景 要解决的问题）。持久化 checkpoint 等于把错误的坐标系固化进 store。
- 引入新 record kind（checkpoint item）与 append-only 留痕哲学冲突（checkpoint 是 derived data，不是原始对话留痕）。
- 推导是纯 CPU 操作，对正常大小的 thread 耗时可忽略；不需要额外存储格式、不需要迁移。
- 推导从逻辑 history 算，坐标系与 engine 后续 `_history[:cut]` 截断完全自洽。

`derive_rewind_log` 成为**冷加载 / 热 turn 结束 / CompactNow 三处的唯一产出方**，消除 rewind re-run 撞号、`_turn_index` 回填依赖、热冷不一致。

### 决策三：node_id 升级为 turn 限定格式

node_id 从 `it{n}` / `disp{m}` 升级为 **`t{k}:it{n}` / `t{k}:disp{m}`**，其中 `k` = 累积 `user_message` 数（1-based），由 `count_turns(history)` helper 从逻辑 history 算出。`RewindCheckpoint` 增加 `turn_index: int` 字段（= k 值）。

**为什么需要 turn 限定**：

- 多 turn thread 中，第 2 turn 的第 1 圈和第 1 turn 的第 1 圈如不加 turn 前缀，node_id 均为 `it1`，无法区分。冷重建历史任意 turn 的节点必须全局唯一。
- `k` 用累积 `user_message` 数而非 `engine._turn_index`：后者起始 0、冷加载不回填，直接用会撞号。用 history 内 `user_message` 计数与 `derive`（从 history 扫）和 live（`TurnRunner` 起跑时从 `history_buffer` 数）两者同源，天然一致。

**破坏性**：已存的 `it2` 这类 id 会与 `t1:it2` 失配。业务侧已确认接受；迁移说明：按 `t1:` 前缀重算即可（热场景历史 thread 如需 rewind，重新加载后 `rewind_nodes()` 返回新格式 id）。

### 决策四：rewind / rollback marker 持久化 `cut_index`

`_handle_rewind` / `_handle_rollback` 落 store 的 marker（`system_injection`）payload 补充 `cut_index` 字段（通过 `system_injection` 的 additive `extra` 参数合并进 payload，不破现有调用）。`reconstruct` 读此字段完成截断。

这是最小改动：只新增一个 payload 字段，不改 store 格式，不破 R5 append-only。

## 影响

- **R1**：`reconstruct_logical_history` / `derive_rewind_log` / `count_turns` 全通用，无业务概念，与任意符合协议的 `MessageStore` 后端解耦。✅
- **R2**：冷加载跨进程 cache 不可信，engine `__init__` 置 `_cache_anchor_index = -1`；derive 的 checkpoint `cache_anchor` 填 -1（纯保险）；rewind apply 走 `cache_break_expected_reason="rewind"`，首采样失效标 expected，不计 `unexpected_cache_breaks`。✅
- **R3**：新增 `RewindTableRebuilt{thread_id, turn_count, node_count}` 事件，在 pool 重建 spawn-state 后 emit；热路径 `rewind_checkpoint_recorded` 不变。冷 rewind 指令 resolve 失败 log warning（不 silent suppress）。✅
- **R4**：`reconstruct` / `derive` 均同步纯 CPU，无长操作，不需要 `CancellationToken`。✅
- **R5**：两函数只读 history，store append-only 不动；marker 补 `cut_index` 是 additive。**新增协议红线**：`reconstruct` + `derive` 依赖 `load_thread` 按写入顺序、完整吐回所有 `ResponseItem`（不去重 marker、不丢、不乱序）。默认 `JsonlMessageStore` 天然满足；业务自实现 DB store 时为此约束。✅

**顺带修正**：`reconstruct` 被引入后，resume 路径也改为先重建逻辑 history，修正了原先「resume 压缩过的 thread 会把废弃项重发给 LLM」的既存隐患（不崩但错误的行为）。

## 排除备选

- **持久化 checkpoint 到 JSONL 新 record kind**：见决策二，坐标系不自洽，引入新格式，代价大于收益。
- **写时修改 JSONL（压缩时删除旧项）**：破坏 R5 append-only，多写点需同时改，且无法约束业务侧 MessageStore，不可行。
- **warmup 时预填充 `_last_resolved`**：冷 rewind 可能远晚于 warmup，惰性 on-rewind resolve 保证使用 rewind 当下的指令层（含热更）；且只在真正 rewind 时付出 resolve 成本。选惰性。

## 参照

设计 spec：`docs/superpowers/specs/2026-06-07-cold-rewind-rebuild-design.md`
