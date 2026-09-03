# Capability: compaction-offload-strategy

## Purpose

压缩谱系唯一的**无损可回溯**档:超大 `function_call_output` 完整落盘、history 原地留 stub 指针,LLM 按需 `file_read` 分页回读。补齐其余三档(Handoff 摘要 / SurgicalTrim 截断 / Sliding 丢弃)对超大结果一律有损的缺口——覆盖「当下不需占 context、稍后可能精确回看」的高频场景(大 JSON / 日志 / 文件内容)。

参照:deepagents `libs/deepagents/.../middleware/_message_eviction.py`(只学范式:落盘 + stub + head/tail 预览;回溯一律 LLM 主动 `file_read`,**不自动 rehydrate**)。差异 Y:以 taifeng `CompressionStrategy` 协议 + 文件沙箱原语重写,挂进 `CompressionOrchestrator`。

实现:`src/taifeng/context/strategies/offload.py`;`context/placeholders.py`(共享占位符守卫);`tool/builtins/file_io.py`(`file_read` offset/limit 回溯通道)。

## 数据契约

### `OffloadStrategy` 构造参数(全部业务注入,R1)

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `file_root` | (必填) | offload 文件沙箱根;应与 `file_read` 工具 `root_dir` 同根 |
| `priority` | 30 | orchestrator 排序;高于 SurgicalTrim(20)——有大结果优先无损落盘 |
| `offload_bytes_threshold` | 8192 | 单条 output 字节数 ≥ 此值才落盘(**独立**于 trim 阈值) |
| `preview_head_lines` / `preview_tail_lines` | 5 / 5 | stub 中 head/tail 预览行数 |

### 落盘路径(确定性派生)

`{file_root}/_offload/{thread_id}/{call_id}` —— 可从 stub 的 `call_id` 纯函数推导,无独立索引。offload 文件独立于 history(JSONL)持久化。

**路径段校验**：`thread_id` 与 `call_id` 都是外部输入（history / provider），拼接前各自 SHALL 通过：非空、不为 `.` / `..`、不含 `/` `\` `\x00`、非绝对路径；且落盘 target 的 resolved 父目录 SHALL 等于 `{file_root}/_offload/{thread_id}` 的 resolved 结果。不通过 → 不落盘、返回 None（该条目保留原文）并记 warning，MUST NOT 写到 `file_root` 之外。

### stub 结构

以 `OFFLOAD_PREFIX`(`[offloaded:`)起头(→ `is_placeholder` 幂等识别):

```
[offloaded: call_id=<id>, <N> bytes saved to _offload/<tid>/<id>]
完整结果已落盘。用 file_read(path="_offload/<tid>/<id>", offset=, limit=) 按行分页回读(勿一次全读)。
预览(head/tail):
<head N 行>
... [K lines truncated] ...
<tail N 行>
```

### `CompressionResult.detail`

本档填 `{"offloaded": int, "bytes_saved": int}`;`turn.py` 组装 `compaction_completed` 事件透传(R3)。

### `file_read` 回溯通道(tool-builtins 增量)

新增可选 `offset`/`limit`(**按行**,0 基);省略时行为与旧版逐字节一致;给定时按 `splitlines` 切片再对结果限幅,**绕过整文件 byte-cap**(否则大文件后段行不可达)。详见 [tool-builtins-extended.md](tool-builtins-extended.md)。

## 行为契约

### Requirement: 触发(独立 bytes 阈值 / 仅 tool-result / 非 pre_turn)
- **WHEN** tail(`index > cache_anchor_index`)中存在超 `offload_bytes_threshold`、有配对 fc、非占位符的 `function_call_output`,且 `phase != pre_turn`
- **THEN** `should_trigger` 返回 `CompressionTrigger`;否则返回 None(让位有损档)

### Requirement: 落盘 + stub 替换
- **WHEN** `compress` 命中候选
- **THEN** 完整内容写确定性路径;history 中该条 output 原地替换为 stub(保留 item id/thread_id);非 tool-result、孤儿 output、已占位符条目跳过

### Requirement: LLM 主动回溯,系统不自动 rehydrate
- **WHEN** LLM 未主动 `file_read` 某 offload 内容
- **THEN** 系统 SHALL NOT 把内容自动注回 history
- **WHEN** LLM 以 stub 路径调用 `file_read(path, offset, limit)`
- **THEN** 返回对应行区间,不触发整文件截断

### Requirement: 幂等 / 失败回退
- **WHEN** provider 返回 `call_id="../../../evil"` 且该条 output 超阈值
- **THEN** 不产生任何 `file_root` 外的文件；该条目保留原文不被替换为 stub
- **WHEN** 再次扫描到已带 `OFFLOAD_PREFIX` 的 stub → 跳过,不二次落盘
- **WHEN** 单条落盘抛 OSError → 保留原始 output,不产半截 stub,继续处理其余候选

### Requirement: R2 cache 友好
- **WHEN** offload 在 mid_turn/overflow 落盘 anchor 之后的 tail
- **THEN** `CompressionResult.cache_invalidated == False`、`anchor_preserved_until` 不前移;`pre_turn` 不 offload

### Requirement: R5 可 resume
- **WHEN** stub 落 JSONL → 新 store replay 重建 → 据确定性路径 `file_read`
- **THEN** 返回 resume 前同一份原文(逐字节一致)

### Requirement: 生命周期(thread 级联清理,v1)
- **WHEN** `cleanup_thread(thread_id)` 调用(thread/conversation 删除时)
- **THEN** 级联删除 `_offload/{thread_id}` 目录;只清目标 thread;缺失目录 noop(幂等);不做 TTL/容量上限

## R1–R5 影响

- **R1**:仅 stdlib + anyio + 文件沙箱;无业务概念。
- **R2**:仅动 anchor 之后 tail,`cache_invalidated=False`。
- **R3**:`detail` 计数透传 `compaction_completed`。
- **R4**:落盘前 `anyio.lowlevel.checkpoint()` 协作取消(`CompressionContext` 不携带 token,与 surgical_trim 同机制)。
- **R5**:stub 落 JSONL + 路径确定性派生 + offload 文件独立持久化。

## 真实 LLM 验证

见 `docs/capability-matrix.md` 对应行 + `docs/real-llm-ledger.md` 台账。

## 决策来源

openspec change `compaction-offload-strategy`(proposal/design/specs);设计岔路(回溯=LLM 主动读、按行分页、独立阈值排 trim 前、仅 thread 级联清理)见 design.md Resolved Questions。
