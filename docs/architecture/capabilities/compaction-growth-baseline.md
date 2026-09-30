# Capability: compaction-growth-baseline

> 状态：Stable（opt-in，默认关闭）。关联 ADR 0004 / 0017（规则①）/ 0036 / 0043 / 0083。
> 实现：`src/taifeng/context/budget.py`（`recompaction_blocked` / `last_compaction_baseline`）、
> `src/taifeng/loop/turn_compaction.py`（设闸与基线落盘）、`loop/event.py::CompactionDeferred`。

## Purpose

压缩腾出的空间有限时（保留的尾部本身就大、钉回的状态多），估算会停在软阈值之上。此后每一次预算检查都会再触发
一次压缩：每次都破坏缓存、每次都对摘要再做摘要，却几乎腾不出空间。本能力以**上次压缩结束时的估算**为基线，
上下文没有在基线之上长出足够多，就不再压。

## 数据契约

### 基线

每次成功且产生 `compacted` 条目的压缩，SHALL 在该条目的 `metadata["post_compaction_tokens"]` 记下压缩应用完成
（含抢救摘要与钉回项）时的上下文估算，并随条目落 transcript。

- 无论是否配置了闸门都记录：之后打开闸门时，已有的压缩就有据可依；
- 估算口径与压缩触发判定相同（`TurnRunner._history_token_estimate`，含实测校准）；
- 就地改写 payload 的策略（`surgical_trim` / `offload` / `multimodal_evict`）不产生条目，不记基线；
- `last_compaction_baseline(history)` 取 history 中**最后一个** `compacted` 条目的基线；没有压缩、该条目没有记录、
  或值不是非负整数时返回 None。

### `ContextBudget.recompact_min_growth_ratio`

有限的非负数，默认 `0.0`（不设闸）；非法值构造期 `ValueError`。可经 `UpdateBudget` 运行时调整。

### `compaction_deferred` 事件

`data = {phase, reason: "below_growth_baseline", token_estimate, baseline_tokens, required_tokens}`。

## Requirements

### Requirement: 增长不足时推迟压缩

`pre_turn` / `mid_turn` 的压缩检查在估算已达软阈值之后、`pre_compact` hook 之前，SHALL 按
`recompaction_blocked(history, token_estimate, budget)` 判定；以下条件同时成立时推迟本次压缩：

1. `recompact_min_growth_ratio > 0`；
2. history 中最后一次压缩记录了基线；
3. `token_estimate < baseline + ceil(baseline × ratio)`；
4. `token_estimate < hard_limit`。

推迟时 SHALL emit `compaction_deferred`，SHALL NOT emit `compaction_started`，history / cache anchor 不变，
`_maybe_compress` 返回 False。

到了硬阈值 SHALL NOT 设闸——不压就要溢出。`manual`（`CompactNow`）与 `overflow` 自愈路径 SHALL NOT 设闸。

#### Scenario: 压缩后只多了一问一答
- **GIVEN** `recompact_min_growth_ratio=0.2`，上次压缩的基线为 828（高于软阈值 750、低于硬阈值 1485）
- **WHEN** 下一轮的预算检查时估算为 830
- **THEN** emit `compaction_deferred{baseline_tokens: 828, required_tokens: 994}`，压缩策略不被调用

#### Scenario: 上下文又长出一大截
- **GIVEN** 同上
- **WHEN** 估算达到 994 以上
- **THEN** 照常压缩，新的压缩条目记下新的基线

#### Scenario: 旧 transcript
- **GIVEN** 压缩条目写于本能力引入之前（没有基线）
- **THEN** 不设闸，行为与引入前一致；下一次压缩起开始记录基线

## R1–R5 影响

- R1：比例由业务注入；无业务概念。
- R2：推迟压缩即保住当前缓存前缀——本能力减少不必要的 cache 失效。
- R3：`compaction_deferred`。
- R4：纯计算。
- R5：基线随压缩条目落 transcript，冷加载后闸门状态与热内存一致。
