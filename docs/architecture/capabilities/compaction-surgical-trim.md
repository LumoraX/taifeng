# Capability: compaction-surgical-trim

## Purpose

介于「滑窗整条丢弃」与「LLM 摘要」之间的**就地有损剪枝**（手术刀档）：dedup → soft-trim → hard-clear 三 pass 按 ratio 分级，全程 LLM-free、只改写 `function_call_output` payload、永不删条目。覆盖「上下文爆到 30%、还不到该 handoff 的程度」的低成本回收窗口，并消除「同一文件反复 read 的重复大输出白烧 token」。

参照：openclaw `pi-hooks/context-pruning/pruner.ts`（soft/hard 分级 + cache-TTL 对齐）+ hermes `agent/context_compressor.py`（md5 去重）。第三轮对比分析 P1 缺口 A2+A3。

实现：`src/taifeng/context/strategies/surgical_trim.py`；`context/compressor.py`（`CompressionResult.detail` 字段）；`loop/turn.py`（`compaction_completed` 事件透传 detail）。

## 数据契约

### `SurgicalTrimStrategy` 构造参数（全部业务注入，R1）

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `priority` | 20 | orchestrator 排序；推荐最高（最便宜先试，剪后仍超下一轮自然落 handoff） |
| `soft_trim_ratio` | 0.3 | ratio ≥ 此值启用 soft-trim（亦是 `should_trigger` 阈值） |
| `hard_clear_ratio` | 0.5 | ratio ≥ 此值 soft 升级为 hard-clear |
| `min_dedup_chars` | 256 | 参与 md5 去重的 output 最小长度（去重恒启用，零成本） |
| `head_chars` / `tail_chars` | 400 / 200 | soft-trim 保留头/尾字数（truncate_middle 复用） |
| `protect_tail_messages` | 4 | 尾部保护条数，窗口内永不剪 |
| `allow_globs` / `deny_globs` | `("*",)` / `()` | 工具名 fnmatch 白/黑名单，deny 优先 |
| `allow_head_clear` | False | 仅 pre_turn 允许 hard-clear 越 cache anchor |
| `cache_ttl_seconds` | None | cache-TTL 对齐触发（opt-in） |
| `clock` | `time.monotonic` | 时间源（测试注假钟） |

### `CompressionResult.detail: dict[str, int]`（协议增量字段）

默认空 dict（既有策略零改动兼容）；本策略填 `{"deduped", "soft_trimmed", "hard_cleared"}`。`turn.py` 组装 `compaction_completed` 事件 data 时透传（键 `detail`）。

### 占位符（幂等守卫前缀）

- 去重：`[duplicate tool output: md5=<12位>, kept latest at #<idx>]`
- hard：`[pruned: tool output cleared, original <N> chars]`

## 行为契约

### Requirement: 三 pass 分级、就地改写、配对安全
- **WHEN** `compress` 执行
- **THEN** 仅对可剪窗口内、glob 命中、非占位符的 `function_call_output` 操作：去重（反扫保最新）恒启用；`soft ≤ ratio < hard` 头尾截断；`ratio ≥ hard` 整体占位符替换。就地替换 payload、不删条目、不触碰配对 `function_call`；孤儿 output（call_id 无配对 fc）跳过。

### Requirement: 窗口与 cache（R2）
- 常规窗口起点 = `cache_anchor_index`（DO_NOT_INJECT 下 anchor 前逐字节不变 → `cache_invalidated=False`）；尾部 `protect_tail_messages` 条永不剪。
- **WHEN** `allow_head_clear=True` 且 BEFORE_LAST_USER_MESSAGE，hard-clear 改写 anchor 前条目
- **THEN** `cache_invalidated=True` 且 `anchor_preserved_until` 如实反映新边界。

### Requirement: 触发与 cache-TTL 闸
- `should_trigger`：`token_estimate / context_window ≥ soft_trim_ratio` 返回 trigger；启用 ttl 时距上次成功剪枝不足 ttl 返回 None；剪枝成功刷新时间戳。

### Requirement: 幂等
- **WHEN** 对刚剪过且无新增内容的 history 再次 compress
- **THEN** 零改写，`success=False, reason="nothing_to_trim"`（占位符前缀守卫 + truncate 天然 no-op）。

### Requirement: 可观测（R3）与取消（R4）
- 明细经 `CompressionResult.detail` → `compaction_completed.data["detail"]` 机读透出。
- pass 边界 `await asyncio.sleep(0)` 协作检查点：外部 task 取消在边界生效（`CompressionContext` 不携带 CancellationToken，context/ 不反向依赖 loop/——这是分层约束下的有意取舍）。

## R1–R5 影响

- **R1**：✅ glob / ratio / ttl / 窗口全构造期注入；md5 指纹与 `ResponseItem` 结构无关，无业务概念。
- **R2**：✅ 核心卖点——常规只动 anchor 后 tail；cache-TTL 模式把有损动作对齐缓存失效时刻；越 anchor 如实标注。
- **R3**：✅ `detail` 三计数经 `compaction_completed` 透传。
- **R4**：✅ pass 边界协作检查点（LLM-free，单 pass 毫秒级）。
- **R5**：✅ 就地替换保 item 身份（`model_copy` 仅换 payload），条目数与顺序不变，resume 重放结构稳定。

## 测试

`tests/context/test_surgical_trim.py`（17 用例：三 pass / 窗口 / glob / 孤儿 / ttl 假钟 / 幂等 / 协作取消——取消测试含负证：无检查点的纯同步实现会跑完导致断言失败）、`tests/loop/test_turn_overflow_recovery.py::test_compaction_completed_carries_strategy_detail`（turn 级 detail 透传接线）。demo：`examples/compression_showcase/surgical_demo.py`（实跑确证 soft/hard 两档 detail）。
