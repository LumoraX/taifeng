# ADR 0036：cache anchor 真值化——含语义统一、采样成功后推进、overflow 两档、坏参数拒绝

- 状态：Accepted
- 日期：2026-09-07
- 关联：[cache-anchor 契约](../architecture/capabilities/cache-anchor.md)；[context-compression 活文档](../architecture/context-compression.md)；[reactive-compaction-recovery 契约](../architecture/capabilities/reactive-compaction-recovery.md)；[tool-whitelist 契约](../architecture/capabilities/tool-whitelist.md)；openspec change `wave2c-cache-anchor-truth`；同一审查波次的 ADR 0028 / 0029 / 0035

## 背景

2026-09-03 全系统审查把 context 组与 loop 组关于 cache anchor 的发现列为 Wave 2c。在 main（e6a4673）逐处核对后确认四个真实缺陷（回归用例 `tests/loop/test_cache_anchor_truth.py`，改动前 11 例全部 FAIL）：

1. **anchor 从不推进**：`TurnRunner.cache_anchor_index` 只在压缩成功时被 `anchor_preserved_until` 回写，engine 初值 -1，字段注释写「业务层在每轮后更新」但全仓无人更新。anchor 是死变量：mid-turn「只动 tail 保 cache」保护的是空集，`build_api_request` 从未产出 `cache_breakpoints`（anthropic `cache_control` 从未打过），`compaction_mid_turn_anchor_lost` 不可能被观测。R2 契约在运行时是假的。
2. **anchor 语义分裂**：契约层（`CompressionContext` docstring、`anchor_preserved_until`「索引（含）」、offload 的 `anchor+1`、三策略返回的 `start-1`、rewind 的 `cut-1`）按**含**语义；消费层（sliding `head_end=anchor`、handoff `start=anchor`、surgical 窗口起点与越界判定 `i < anchor`、prompt 映射 `< anchor` 且 `anchor > 0`）按**不含**语义。anchor 一旦活过来，anchor 本条会在 mid-turn 被改写，策略却报 `cache_invalidated=False`，真实 cache break 记成 `unknown_drop`。
3. **坏 JSON 参数静默变 `{}`**：turn 主派发、rewind retry_tool 补跑、engine 两处 resumed tool 四处复制 `except JSONDecodeError: arguments = {}` 后照常执行 handler；非对象 JSON 原样穿透到 handler。模型永远不知道自己的参数格式错了。
4. **anchor 活化后的连带风险**：overflow 自愈只走 `DO_NOT_INJECT`。anchor 恒 -1 时等价于全量压缩；活值化后只能动最后一圈的 tail，`boundary_too_narrow` 时 handoff / sliding 还会因非法区间抛 `ValueError` 直接打死 turn——比现状更弱。

## 决策

### 1. anchor = 最后一条已缓存 history 下标（含），-1 = 无缓存

首个可变下标 = `anchor + 1`。选「含」是最小改动：契约层已全是含语义，只有四个消费点写反；且 -1 自然表示「无」（不含语义下 0 与「无」歧义）。sliding / handoff / surgical 窗口起点改 `anchor + 1`，surgical 越界判定改 `i <= anchor`，prompt 映射改 `anchor >= 0` 打点、来源下标 `<= anchor`。

### 2. 采样成功后推进：anchor = 发出时 history 长度 - 1

`_sample_once` 记发出请求时的 `history_buffer` 长度，ResponseEvent 流正常完成后推进。取发出时长度而非完成后长度：provider 缓存的是它收到的前缀，本轮产出（assistant / fc）尚未进缓存。LLMError / overflow / 取消路径不经此处，不推进。回退规则不变：pre_turn / manual 压缩 head → -1；rewind → `cut - 1`；跨进程重载 → -1。

### 3. overflow 自愈两档

第一档沿用 `DO_NOT_INJECT`（只动 anchor 后 tail、保 cache）；第一档未应用（无策略 / 策略失败 / 边界过窄 / 完整性回滚）且 anchor ≥ 0 时进第二档 `allow_head=True`（`BEFORE_LAST_USER_MESSAGE` 语义），策略如实报 `cache_invalidated=True`，turn 把随后的 cache break 标 expected、reason `compaction_overflow`。重采样仍恰一次。`_maybe_compress` 返回「是否应用」作为分档判据。sliding / handoff 对不足两条的区间先判 `boundary_too_narrow`，不再让 `resolve_compaction_range` 抛 `ValueError`。

### 4. 坏参数是派发层裁决 `invalid_arguments`，不是解析层兜底

`tool_batch.parse_tool_arguments` 是唯一解析入口（空串 → `{}`；非法 JSON / 非对象 → 携带错误）；`ToolCallRequest.arguments_error` 把解析与裁决分离；`_dispatch_one` 在 `not_offered` 之后、hook 之前以 `ToolResult.error("invalid_arguments: ...", reason="invalid_arguments")` 核销 call_id（与 `not_offered` 同构，抽 `_reject_before_dispatch`）。四处解析点全部改走入口；resumed tool 路径出错即以 error fco 结算不 dispatch；audit 分类归 `rejected`。

## 替代方案

- **改契约为不含语义**：要改 compressor 契约、offload、三策略返回值、rewind、engine 初值，且 0 与「无缓存」歧义；否决。
- **anchor 推进到流完成后的长度**：把本轮产出算进已缓存前缀，下一轮 mid-turn 会把尚未被 provider 缓存的条目当不可动；否决。
- **overflow 第二档手动把 anchor 拨 -1 再走 DO_NOT_INJECT**：策略会报 `cache_invalidated=False`，与事实相悖，还要在 turn 手工置 expected；否决，改传注入语义让策略自己如实报。
- **overflow 直接一档 head 可动**：大 tool 输出多半在最后一圈 tail，剪掉即可救活且不破 cache；一档全量压缩既丢 cache 又丢细节；否决。
- **坏参数在 provider 层修正 / 丢弃**：provider 不知道工具 schema，且四条路径中三条不经 provider；否决。

## 后果

- mid-turn 压缩范围收窄到最后一圈 tail（anchor 之后）；每圈起始的 pre_turn 检查仍可全量压缩，整体压缩能力不减。
- anthropic 路径开始真正携带 `cache_control`（打在上次发出的末条消息）。
- `CacheBreakReason` 新增 `compaction_overflow`、`rewind`。
- `ToolCallRequest` 新字段 `arguments_error`；`ToolResult.data.reason` 新取值 `invalid_arguments`；模型可见错误正文 `invalid_arguments: <detail>`。
- provider 端把历史 fc 参数回放为 `{}` 的容错（anthropic / gemini `_to_*_messages`）留待 Wave 3 provider 对齐。
