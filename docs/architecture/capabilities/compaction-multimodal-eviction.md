# Capability: compaction-multimodal-eviction

> 状态：Experimental。关联 ADR 0004 / 0017（规则①）/ 0036 / 0082。
> 实现：`src/taifeng/context/strategies/multimodal_evict.py`；经 `taifeng.experimental` 导出。

## Purpose

把**旧条目**上的图片 / 文件附件换成一行描述，文本原样保留，最近的若干条带附件条目不动。一张图片折合上千 token，
被模型看过之后对后续推理的价值远低于它占用的上下文。全程 LLM-free。

与其他档的分工：`surgical_trim` 剪工具**文本**输出（附件只在文本被剪时顺带丢掉）；`offload` 回避带附件的条目；
用户消息上的附件、文本很短却带着大图的工具结果，只有本策略处理。

## 构造参数

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `priority` | 25 | 介于 `surgical_trim`（20）与 `offload`（30）之间 |
| `trigger_ratio` | 0.3 | `token_estimate / context_window` ≥ 此值才可能触发；取值 (0, 1] |
| `keep_recent` | 2 | 保留最近多少条**带附件的条目**不动（按条目计） |
| `protect_tail_messages` | 4 | 最后 N 条内的附件永不驱逐，也不占 `keep_recent` 的名额 |
| `min_attachment_bytes` | 0 | 只驱逐声明大小 ≥ 此值的附件；大小缺失视为可驱逐 |
| `allow_head_evict` | False | 仅 pre_turn 下允许越过 cache anchor |

参数越界构造期 `ValueError`。

## Requirements

### Requirement: 只改写 payload，不增删条目

`compress` SHALL 就地改写候选条目的 payload，SHALL NOT 增删条目或改变条目 id / 顺序；SHALL NOT 修改入参 history。

- 可带附件的条目：`user_message`（文本键 `text`）、`function_call_output`（文本键 `output`）；
- 被驱逐的附件从 `attachments` 中移除，在文本后另起一行追加
  `[evicted: N attachment(s): <类型> <大小>KB [<文件名>] sha256=<前 8 位>; ...]`；原文本为空时只有这一行；
- 附件缺 `media_type` / `size` 时描述里写 `unknown type` / `unknown size`，不编造；
- 驱逐后的 payload 形状与「本就没有附件」的条目逐键一致：`user_message` 保留 `attachments: []`，
  `function_call_output` 不写 `attachments` 键；同一条目上未被驱逐的附件（小于 `min_attachment_bytes`）保留；
- 描述前缀 `EVICTED_PREFIX`（`[evicted:`）不属于 `is_placeholder` 的识别集——它追加在文本之后，带着它的文本仍是
  可被其他档剪枝的正常输出。

#### Scenario: 驱逐旧附件、保留最近的
- **GIVEN** history 中有四条带附件的条目，`keep_recent=1`
- **WHEN** 触发并执行
- **THEN** 前三条的附件被换成描述，最后一条原样保留；条目数、条目 id、调用配对不变
- **AND** `detail == {evicted_items: 3, evicted_attachments: <数量>, evicted_bytes: <声明字节数之和>}`

### Requirement: 候选窗口与 cache anchor（R2）

候选 = 窗口 `[start, len − protect_tail_messages)` 内带可驱逐附件的条目，去掉其中最近的 `keep_recent` 条。

- 常规 `start = cache_anchor_index + 1`：已缓存前缀不动，`cache_invalidated=False`；
- `allow_head_evict=True` 且注入语义为 `BEFORE_LAST_USER_MESSAGE`（pre_turn / manual）时 `start = 0`；改写了下标
  `<= cache_anchor_index` 的条目即 `cache_invalidated=True`，`anchor_preserved_until` = 最小被改写下标 − 1。

### Requirement: 没有可驱逐的附件时不触发

`should_trigger` SHALL 在 `ratio < trigger_ratio` 或窗口内没有候选时返回 None。orchestrator 只执行第一个触发的
策略，空转会挡住后面真正能腾出空间的策略。

`compress` 在没有候选时返回 `success=False, reason="nothing_to_evict"`（`force_compress` 路径可能走到）。

### Requirement: 幂等与取消

- 二次执行无候选（附件已不在），不产生改写；
- 每处理 16 条与结束前各有一个 `await asyncio.sleep(0)` 协作检查点（R4）。

## 与持久化的关系

与 `surgical_trim` / `offload` 相同：改写只发生在内存 history，transcript 保持 append-only、原件仍在。冷加载后附件
回到 history，下一次触发时再次驱逐。业务可据描述里的 sha256 前缀从自己的存储取回原件。

## R1–R5 影响

- R1：阈值与保留数量由业务注入；无业务概念。
- R2：见「候选窗口与 cache anchor」。
- R3：`detail` 经 `compaction_completed` 透出。
- R4：协作式取消检查点。
- R5：不改 transcript；条目身份与顺序不变。
