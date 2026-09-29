# Capability: compaction-background

> 状态：Experimental。关联 ADR 0004 / 0017（规则①）/ 0036 / 0087。
> 实现：`src/taifeng/context/strategies/background.py`；`loop/pool_lifecycle.py`（关闭时收尾）。
> 经 `taifeng.experimental` 导出。

## Purpose

LLM 摘要类压缩要调一次模型，耗时数秒到数十秒。按既有做法它发生在某一轮开始之前，用户提交消息后要先等压缩跑完。
`BackgroundCompactionStrategy` 把这次计算挪到后台：到软阈值时开始算、当前这一轮照常进行；下一轮开始时若 history
前缀没变，直接应用算好的结果。

## 用法

```python
compressors=[BackgroundCompactionStrategy([HandoffCompactionStrategy(...), SlidingWindowStrategy()])]
budget=ContextBudget(soft_limit_ratio=0.6, hard_limit_ratio=0.95)   # 软阈值调低，给后台留出时间
```

本策略 SHALL 作为 `compressors` 里**唯一**的策略：外层 orchestrator 在一个策略不触发时会继续尝试下一个，并列配置的
兜底策略会在后台计算期间立刻同步压缩。兜底策略应放进本策略的内层列表。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `strategies` | 必填，非空 | 内层策略，按各自 `priority` 排序 |
| `priority` | 100 | 在外层 orchestrator 里的优先级 |
| `urgent_ratio` | 0.85 | 估算占窗口比例 ≥ 此值时不再等后台，同步压缩；取值 (0, 1] |

## Requirements

### Requirement: pre_turn 的三分支

估算已达软阈值、且内层有策略愿意压缩时，`should_trigger` SHALL 按序判定：

1. 该 thread 有算好的后台结果，且其快照仍是当前 history 的前缀（逐条目 id 比对）→ 触发；`compress` 直接返回
   该结果，**不再调用内层**；
2. 估算 ≥ `urgent_ratio × context_window`、策略已关闭、或该 thread 后台失败过 → 触发；`compress` 交给内层同步
   压缩；在算的后台任务作废；
3. 其余 → 保证该 thread 上有一个基于当前前缀的后台任务，返回 None（本轮不压缩）。

内层没有策略愿意压缩时恒返回 None，不起后台任务。

#### Scenario: 当前这一轮不被压缩阻塞
- **GIVEN** 内层摘要需要很久
- **WHEN** 估算越过软阈值后的几轮对话
- **THEN** 每一轮都照常完成，history 尚无 `compacted` 条目；后台任务已开始

#### Scenario: 下一轮开始时应用
- **GIVEN** 后台摘要已算完，期间 history 又追加了若干条目
- **WHEN** 下一轮的 pre_turn 检查
- **THEN** `compaction_completed.detail.background == 1`；内层没有被再次调用
- **AND** 新 history = 压缩结果 + 快照之后追加的条目（原条目、原顺序）；`compacted` 条目落 transcript 并带增量基线

### Requirement: 快照与前缀

后台任务 SHALL 在发起时刻的 history 快照上计算，注入语义为 `BEFORE_LAST_USER_MESSAGE`。应用时把快照之后追加的条目
原样接在 `new_history` 后面；`summary_item_id` / `cache_invalidated` / `anchor_preserved_until` 沿用计算结果。

当前 history 不再以快照为前缀（rewind / rollback / 另一次压缩改写过）时，结果 SHALL 作废，按分支 3 重新开始。

### Requirement: mid_turn 不起后台、不应用后台结果

后台结果按 pre_turn 语义算出，可能改写已缓存的前缀。`phase != "pre_turn"` 时：估算未达 `urgent_ratio` 返回 None；
已达则触发并交给内层按该 phase 的语义同步压缩。

### Requirement: 失败处理

后台任务抛异常（记 error 日志含堆栈）、返回 `success=False`、或内层无策略触发时，该结果作废，并把该 thread 标记为
「后台失败过」——下一次 pre_turn 检查走分支 2 同步压缩，结果如实返回。任一次 `compress` 调用之后清除该标记。

### Requirement: 隔离、唯一与关闭

- 状态按 thread 隔离（`history[0].thread_id`），同一策略实例可被多个 engine 共用；
- 每个 thread 至多一个后台任务；前缀未变时重复检查不重复发起；
- `aclose()` 取消全部后台任务并清空状态，此后一律同步；`EnginePool.close()` SHALL 对每个提供 `aclose` 的压缩策略调用它；
- `wait_idle()` 等在算的任务收尾，不取消；`pending_threads` 给出有任务或有待应用结果的 thread。

### Requirement: 强制压缩

`CompressionOrchestrator.force_compress`（overflow 自愈）直接调用 `compress`：有可应用的后台结果（仅 pre_turn）则应用，
否则交给内层 `force_compress` 同步压缩。

## 与其他能力的关系

- **增量基线**（compaction-growth-baseline）：闸门在策略选择之前判定，被推迟时本策略不会被调用；应用后台结果时
  照常记基线。
- **pre_compact hook**：在策略选择之前执行。起后台任务的那次检查同样先过 hook；hook 拒绝则不起任务。
- **回访节点**（turn-rewind）：应用后的 `compacted` 条目与同步压缩的产物无异，照常成为 compaction 节点。

## R1–R5 影响

- R1：阈值与内层策略由业务注入。
- R2：只在 pre_turn 应用；`cache_invalidated` 如实沿用计算结果。
- R3：`compaction_completed.detail.background`；后台失败有 error 日志。后台任务的开始与结束不单独发事件——
  事件需要所属 submission，而后台任务跨 turn 存活。
- R4：后台任务随 `aclose()` / pool 关闭取消；内层策略自身的取消语义不变。
- R5：未应用的后台结果只在内存；进程退出即丢失，冷加载后按常规重新判定。
