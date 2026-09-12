# Capability: reactive-compaction-recovery

## Purpose

turn 内一次 LLM 采样被 provider 以「上下文超长」（`ContextOverflowError`）拒绝时，不立即硬失败丢整个 turn，而是做一次**有界自愈**：强制压缩一次 + 重采样一次，仍失败才硬失败。覆盖「本地 token 估算偏低、provider 已判超长」的窗口（多模态 / provider 计费差异 / 模板膨胀导致本地估算乐观）。

参照：openclaw `pi-embedded-subscribe.ts` 的 `pendingCompactionRetry`（overflow → 记 compaction debt → 强制压缩 → 放行重采样）。第三轮 codex/openclaw/hermes 对比分析 P0 缺口 A1。

实现：`src/taifeng/context/compressor.py`（`CompressionOrchestrator.force_compress`）、`src/taifeng/loop/turn.py`（`_sample_once` overflow 自愈两档分支 + `_overflow_recovered` + `_maybe_compress(bypass_trigger=..., allow_head=...)`）、`src/taifeng/loop/event.py`（`ProviderRetry`）。
变更提案：`openspec/changes/reactive-compaction-recovery/`。

## 数据契约

### `CompressionOrchestrator.force_compress(ctx, injection) -> CompressionResult | None`
绕过各策略 `should_trigger`，以最高优先级策略（`_strategies[0]`，按 priority 倒序）直接 `compress`。无策略返回 None。存在理由：overflow 成因即本地估算偏低 → 各策略 `should_trigger` 必返回 None → `maybe_compress` 压不动，必须强制。

### `ProviderRetry`（`loop/event.py`，`kind="provider_retry"`）
两类来源共用同一事件，按 `reason` 区分（ADR 0039）：

| 字段 | 含义 |
| --- | --- |
| `data.reason` | `context_overflow`（本契约的 overflow 自愈）或触发网络退避重试的 `LLMError.kind`（`transient_network` / `rate_limit` / `server_error` …，由 `RetryingModelClient` 发起） |
| `data.iteration` | 发生自愈 / 重试的采样圈序号（1-based） |
| `data.attempt` / `data.max_attempts` | **仅网络重试**：刚失败的 attempt 序号（1-based）与本次 `stream` 的上限 |
| `data.delay_seconds` | **仅网络重试**：本次退避时长（退避算法与服务端 hint 取较大者）；事件在退避**之前** emit |
| `data.failure_class` / `data.error_kind` | **仅网络重试**：稳定失败分类 / 异常类名 |
| `data.transport_phase` / `data.retry_after_seconds` | **仅网络重试**：传输相位（`connect` / `stream`，仅 `TransientNetworkError`）/ 服务端 `retry_after` 提示秒数；无则 `null` |

网络重试路径的行为契约见 [llm-client 活文档 §RetryingModelClient](../llm-client.md)；本契约下文只描述 overflow 自愈。

### `_maybe_compress(phase, force, bypass_trigger, allow_head) -> bool`（`loop/turn.py`）
- `phase="overflow"`：`CompressionPhase` 取值；默认注入语义同 mid_turn（`DO_NOT_INJECT`，只动 `anchor+1` 起的 tail、保 cache anchor）。
- `bypass_trigger=True`：走 `orchestrator.force_compress` 而非 `maybe_compress`。
- `allow_head=True`：把注入语义切到 `BEFORE_LAST_USER_MESSAGE`（允许动 head）——overflow 第二档专用。
- 返回值：本轮是否**应用**了压缩（无压缩器 / hook 拒绝 / 策略失败 / 完整性回滚均为 False），自愈据此分档。

## 行为契约

### Requirement: Overflow 触发有界自愈
- **WHEN** 采样抛 `ContextOverflowError`、本 turn 未自愈过、已配置压缩器
- **THEN** emit `provider_retry` → 强制压缩（绕阈值）→ 重采样一次；成功则 turn 正常继续

### Requirement: 有界一次
- **WHEN** 同一 turn 内重采样后再次 overflow
- **THEN** 不再压缩重试，硬失败上抛，`turn_failed.data.failure_class == "context_window"`

### Requirement: 无压缩器不浪费重采样
- **WHEN** overflow 但 `compressors is None`
- **THEN** 直接硬失败，不重采样、不发 `provider_retry`

### Requirement: 压缩失败退化
- **WHEN** 强制压缩无可应用结果（无策略 / 失败 / G1b 配对回滚）
- **THEN** history 不变；重采样再 overflow 后按「有界」硬失败（不引入新失败模式）

### Requirement: Cache 友好两档且可观测
- **WHEN** 自愈发生
- **THEN** 第一档压缩走 `DO_NOT_INJECT`、`history[0..anchor]` 不改写、`CompressionResult.cache_invalidated == False`；emit `provider_retry` + phase=overflow 的 `compaction_started/completed`
- **WHEN** 第一档未应用（无策略 / 策略失败 / `boundary_too_narrow` / 完整性回滚）且 `cache_anchor_index >= 0`
- **THEN** 第二档以 `allow_head=True` 允许动 head，策略如实报 `cache_invalidated=True`，随后的 cache break 标 expected、reason `compaction_overflow`；两档共用同一次自愈机会，重采样仍恰一次。sliding / handoff 对不足两条的可压区间返回 `boundary_too_narrow`（不抛 `ValueError`）

### Requirement: 自愈尊重取消
- **WHEN** 自愈进行中 `CancellationToken` 被取消
- **THEN** 终止自愈、不完成重采样，按既有取消路径 `end_reason=cancelled`

## R1–R5 影响

- **R1**：✅ 纯机制（overflow 既有分类异常 + 压缩/重采样在 loop+context 层，无业务概念）。
- **R2**：✅ 正面。第一档只动 `anchor+1` 起的 tail、保 cache anchor；第二档蓄意破 cache 时如实标 expected（`compaction_overflow`），不污染 `unexpected_cache_breaks`。
- **R3**：✅ `provider_retry` + phase=overflow 压缩事件。
- **R4**：✅ 重采样接收同一 `CancellationToken`。
- **R5**：⚪ turn 内瞬态，无新增持久态。

## 测试

`tests/loop/test_turn_overflow_recovery.py`（触发+重采样 / 有界一次 / 无压缩器 / cache-aware）、`tests/loop/test_cache_anchor_truth.py::test_overflow_second_stage_compacts_head_when_tail_too_narrow`（第二档）、`tests/context/test_compaction.py::test_force_compress_bypasses_should_trigger`。

### 真实 LLM 验证（受限，已如实记录）

`examples/real_llm/p0_verify.py::verify_overflow` 尝试用真实 provider 触发 context overflow：把 budget 调高于估算 128k（本地不预压），塞 ~480k 字符超长上下文。**结果：未触发**——真实 model（`gemini-3.1-pro-preview`）实际 context window 远超 `_provider_bootstrap` 的估算 128k（1M+），该输入正常完成。真实触发需 > 真实 context（>1M token，请求体与成本均不划算）。

故 A1 自愈以 **mock 充分覆盖**为准（force_compress 绕阈值 / 有界一次 / 退化 / cache-aware 五场景）；错误分类链路（`providers/_shared.py` 的 context-overflow 关键字 `exceed`/`context length`/`too long` → `ContextOverflowError`）已核实存在，是真实 overflow → 自愈的衔接保证。

**真实验证还发现并修复了一个 mock 抓不到的内核缺陷**：handoff 压缩的 LLM 调用此前用 `model=self._model or "auto"`，在不认 "auto" 的网关（new-api distributor）会 `model_not_found` → 真实压缩全部失败。**因 A1 force_compress 走同一 handoff 路径，该 bug 会让 A1 在真实网关下也失效**。已修 `"auto" → ""`（对齐采样 `entry_skill.model or ""`，让 provider 用构造默认 model），见 `context/strategies/handoff.py`。修复后真实 gemini handoff 摘要成功（`verify_local_compaction`：本地 budget 到达上限主动压缩 pre_turn × 2、success=True、removed 4/3 条；这也佐证了「到达配置上限即主动压缩、不依赖 provider overflow」的常态路径）。
