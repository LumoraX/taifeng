# Capability: compaction-multimodal-eviction

> 状态：Experimental。关联 ADR 0004 / 0017（规则①）/ 0036 / 0082 / 0147。
> 实现：`src/taifeng/context/strategies/multimodal_evict.py`、`src/taifeng/context/attachment_offload.py`；经 `taifeng.experimental` 导出。

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
| `file_root` / `workspace` | 都不给 | 至多给一个。给了则被驱逐的原件先落盘、描述带路径（见「可恢复」）；都不给则只留描述 |

参数越界、或 `file_root` 与 `workspace` 同时给出，构造期 `ValueError`。

> **何时被询问**：回合内（pre_turn / mid_turn）的压缩检查以 `ContextBudget.soft_limit_ratio`（默认 0.85）为闸，
> 低于它任何策略都不会被询问；过闸后按 `priority` 倒序、第一个触发的策略执行。`HandoffCompactionStrategy` 的
> priority 是 100，过闸即触发 —— 与它并用时，要让本策略先于摘要执行，须把本策略的 `priority` 配到 100 以上，
> 或把 `soft_limit_ratio` 调低到本策略的工作区间。

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

### Requirement: 可恢复（ADR 0147）

配置了 `file_root` / `workspace` 时，`compress` SHALL 在移除附件之前把原件写进工作区，并在描述里留下路径：

- 路径 `_offload/{thread_id}/att-<sha256 前 16 位><扩展名>`（PNG / JPEG / WebP / GIF / PDF 各有扩展名，其余 `.bin`）；
  与 `OffloadStrategy` 共用目录。按内容寻址：目标已存在则不重写；声明的摘要不合法时由字节现算；
- 每条描述末尾追加 `path=<相对路径>`；至少一条落盘成功时，在 `[evicted: ...]` 行之后另起一行写明用
  `file_read(path=...)` 读取；
- 描述里的文件名先做替换（`;` → `,`、`[` `]` → `(` `)`、换行 → 空格），保证「`; ` 分隔、`]` 结尾」可被
  `collect_attachment_pointers` 稳定解析；
- 落盘不了（`thread_id` 不是安全路径段、正文缺失或不是合法 base64、工作区解析 / 写入 `OSError`）SHALL 仍驱逐该附件，
  描述不带路径，并记 warning；
- `detail` 多一项 `offloaded_attachments`（落盘成功数）。未配置工作区时 `detail` 与描述 SHALL 与原契约逐键 / 逐字节一致；
- `cleanup_thread(thread_id)` 删除该 thread 的落盘目录（与 `OffloadStrategy.cleanup_thread` 同一目录；未配置工作区为 noop）。

模型读回图片需要 `make_file_read_tool(attach_images=True)` 与 enabled 的 `ImageInputPolicy`
（[tool-image-attachment](tool-image-attachment.md)）。PDF 等文件附件有路径但不能经 `file_read` 作为附件返回。

#### Scenario: 驱逐后读回原图
- **GIVEN** 一条带 PNG 的用户消息，策略配置了工作区
- **WHEN** 驱逐执行，随后以描述里的 `path` 调用 `file_read(attach_images=True)`
- **THEN** 工具结果带一张图片附件，其 sha256 与字节与原件一致

## 与持久化的关系

与 `surgical_trim` / `offload` 相同：改写只发生在内存 history，transcript 保持 append-only、原件仍在。冷加载后附件
回到 history，下一次触发时再次驱逐（配置了工作区时指向同一个落盘文件）。未配置工作区时，业务可据描述里的
sha256 前缀从自己的存储取回原件。

## R1–R5 影响

- R1：阈值与保留数量由业务注入；无业务概念。
- R2：见「候选窗口与 cache anchor」。
- R3：`detail` 经 `compaction_completed` 透出（含 `offloaded_attachments`）。
- R4：协作式取消检查点。
- R5：不改 transcript；条目身份与顺序不变。
