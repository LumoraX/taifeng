# Capability: cache-anchor（prompt cache 锚点）

## Purpose

`cache_anchor_index` 是 R2「cache 友好」的运行时真相：它标出 history 中哪段前缀已被 provider 缓存，压缩策略据它划「不可动的 head」，`build_api_request` 据它打 `CacheBreakpoint`（anthropic `cache_control`）。本契约固定它的语义、产生点与消费规则；此前它只有消费者没有生产者（永为 -1），且消费者之间语义分裂（ADR 0036）。

实现：`src/taifeng/loop/turn.py`（`_sample_once` 推进、`_maybe_compress` 回写）、`src/taifeng/loop/prompt.py`（坐标映射）、`src/taifeng/context/strategies/{sliding,handoff,surgical_trim,offload}.py`（窗口消费）、`src/taifeng/loop/engine.py`（rewind 回退、跨进程重置）。

## 数据契约

| 结构 | 模块 | 语义 |
| --- | --- | --- |
| `TurnRunner.cache_anchor_index` / `AgentEngine._cache_anchor_index` | `loop/turn.py` / `loop/engine.py` | history 中**最后一条已被 provider 缓存的条目下标（含）**；`-1` = 无已缓存前缀；首个可变下标 = `anchor + 1` |
| `CompressionContext.cache_anchor_index` | `context/compressor.py` | 同上；策略 MUST 只改写下标 `>= anchor + 1` 的条目（DO_NOT_INJECT 下） |
| `CompressionResult.anchor_preserved_until` | `context/compressor.py` | 压缩后仍成立的 anchor（含）；只动 tail 的策略原样返回，动了 head 返回 -1 或新边界 |
| `CacheBreakpoint.index` | `llm/types.py` | messages 下标；`build_api_request` 把 anchor 映射为「最后一条来源 history 下标 `<= anchor` 的产出消息」，`anchor == -1` 或前缀无产出消息则不打点 |
| `CacheBreakReason` | `context/cache_stats.py` | 预期内失效归因：`compaction_pre_turn` / `compaction_manual` / `compaction_overflow` / `rewind` / `skill_snapshot_changed` / `tool_spec_changed` / `system_prompt_changed`；`compaction_mid_turn_anchor_lost` 与 `unknown_drop` 为异常 |

## 行为契约

### Requirement: 采样成功后推进
- **WHEN** 一次 LLM 采样的 ResponseEvent 流正常完成（收到 `completed`，无 LLMError / 取消）
- **THEN** `cache_anchor_index = 发出请求时 history_buffer 长度 - 1`；本轮产出的 assistant / function_call 不计入（provider 尚未缓存）；LLMError / overflow / 取消路径不推进

### Requirement: 策略窗口自 anchor+1 起
- **WHEN** 任一策略以 `DO_NOT_INJECT` 语义压缩
- **THEN** `history[0..anchor]` 逐字节不变；sliding / handoff 的 compactable 区间与 surgical / offload 的候选窗口起点均为 `anchor + 1`；surgical 越 anchor 判定以「改写了下标 `<= anchor` 的条目」为准

### Requirement: 回退规则
- pre_turn / manual 压缩（`BEFORE_LAST_USER_MESSAGE`）改写 head → `anchor_preserved_until = -1` 并标 expected break
- Rewind 截断到 `cut` → `anchor = min(anchor, cut - 1)`，首采样 break 标 expected（`rewind`）
- engine 跨进程重载（`initial_history`）→ `-1`（provider cache 跨进程不可信），首采样成功后才推进

### Requirement: 坐标映射含语义
- **WHEN** `anchor >= 0`
- **THEN** `cache_breakpoints` 恰一个，落在最后一条来源下标 `<= anchor` 的产出消息；前缀全是记账 item 或 `anchor == -1` 时为空

## 与 overflow 自愈的关系

见 [reactive-compaction-recovery](reactive-compaction-recovery.md)：第一档只动 anchor 后 tail；不够救且 anchor ≥ 0 时第二档允许动 head，break 标 `compaction_overflow`。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| R1 | 纯内核机制 |
| R2 | 本契约即 R2 的运行时兑现：anchor 有真值后 mid-turn 才真正只动 tail |
| R3 | `compaction_completed.cache_invalidated` 如实；`cache_break_detected.reason` 归因可区分 overflow 第二档 / rewind |
| R4 | 无变化 |
| R5 | anchor 不落盘；冷加载以 -1 起 |

## 测试

`tests/loop/test_cache_anchor_truth.py`（推进 / mid_turn ctx 真值 / 三策略窗口 / 映射 / overflow 两档）、`tests/loop/test_cache_anchor_mapping.py`、`tests/context/test_compaction.py`、`tests/context/test_surgical_trim.py`、`tests/context/test_offload_strategy.py`。
