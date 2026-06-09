# Capability: reactive-compaction-recovery

## Purpose

turn 内一次 LLM 采样被 provider 以「上下文超长」（`ContextOverflowError`）拒绝时，不立即硬失败丢整个 turn，而是做一次**有界自愈**：强制压缩一次 + 重采样一次，仍失败才硬失败。覆盖「本地 token 估算偏低、provider 已判超长」的窗口（多模态 / provider 计费差异 / 模板膨胀导致本地估算乐观）。

参照：openclaw `pi-embedded-subscribe.ts` 的 `pendingCompactionRetry`（overflow → 记 compaction debt → 强制压缩 → 放行重采样）。第三轮 codex/openclaw/hermes 对比分析 P0 缺口 A1。

实现：`src/taifeng/context/compressor.py`（`CompressionOrchestrator.force_compress`）、`src/taifeng/loop/turn.py`（`_sample_once` overflow 自愈分支 + `_overflow_recovered` + `_maybe_compress(bypass_trigger=...)`）、`src/taifeng/loop/event.py`（`ProviderRetry`）。
变更提案：`openspec/changes/reactive-compaction-recovery/`。

## 数据契约

### `CompressionOrchestrator.force_compress(ctx, injection) -> CompressionResult | None`
绕过各策略 `should_trigger`，以最高优先级策略（`_strategies[0]`，按 priority 倒序）直接 `compress`。无策略返回 None。存在理由：overflow 成因即本地估算偏低 → 各策略 `should_trigger` 必返回 None → `maybe_compress` 压不动，必须强制。

### `ProviderRetry`（`loop/event.py`，`kind="provider_retry"`）
| 字段 | 含义 |
| --- | --- |
| `data.reason` | 重试原因，当前取值 `context_overflow` |
| `data.iteration` | 发生自愈的采样圈序号 |

### `_maybe_compress(phase, force, bypass_trigger)`（`loop/turn.py`）
- `phase="overflow"`：新增的 `CompressionPhase` 取值；注入语义同 mid_turn（`DO_NOT_INJECT`，保 cache anchor）。
- `bypass_trigger=True`：走 `orchestrator.force_compress` 而非 `maybe_compress`。

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

### Requirement: Cache 友好且可观测
- **WHEN** 自愈发生
- **THEN** 压缩走 `DO_NOT_INJECT`、cache anchor 之前历史不改写、`CompressionResult.cache_invalidated` 如实标注；emit `provider_retry` + phase=overflow 的 `compaction_started/completed`

### Requirement: 自愈尊重取消
- **WHEN** 自愈进行中 `CancellationToken` 被取消
- **THEN** 终止自愈、不完成重采样，按既有取消路径 `end_reason=cancelled`

## R1–R5 影响

- **R1**：✅ 纯机制（overflow 既有分类异常 + 压缩/重采样在 loop+context 层，无业务概念）。
- **R2**：✅ 正面。force 压缩 mid-turn 语义只动 tail、保 cache anchor。
- **R3**：✅ `provider_retry` + phase=overflow 压缩事件。
- **R4**：✅ 重采样接收同一 `CancellationToken`。
- **R5**：⚪ turn 内瞬态，无新增持久态。

## 测试

`tests/loop/test_turn_overflow_recovery.py`（触发+重采样 / 有界一次 / 无压缩器 / cache-aware）、`tests/context/test_compaction.py::test_force_compress_bypasses_should_trigger`。

### 真实 LLM 验证（受限，已如实记录）

`examples/real_llm/p0_verify.py::verify_overflow` 尝试用真实 provider 触发 context overflow：把 budget 调高于估算 128k（本地不预压），塞 ~480k 字符超长上下文。**结果：未触发**——真实 model（`gemini-3.1-pro-preview`）实际 context window 远超 `_provider_bootstrap` 的估算 128k（1M+），该输入正常完成。真实触发需 > 真实 context（>1M token，请求体与成本均不划算）。

故 A1 自愈以 **mock 充分覆盖**为准（force_compress 绕阈值 / 有界一次 / 退化 / cache-aware 五场景）；错误分类链路（`providers/_shared.py` 的 context-overflow 关键字 `exceed`/`context length`/`too long` → `ContextOverflowError`）已核实存在，是真实 overflow → 自愈的衔接保证。

**真实验证还发现并修复了一个 mock 抓不到的内核缺陷**：handoff 压缩的 LLM 调用此前用 `model=self._model or "auto"`，在不认 "auto" 的网关（new-api distributor）会 `model_not_found` → 真实压缩全部失败。**因 A1 force_compress 走同一 handoff 路径，该 bug 会让 A1 在真实网关下也失效**。已修 `"auto" → ""`（对齐采样 `entry_skill.model or ""`，让 provider 用构造默认 model），见 `context/strategies/handoff.py`。修复后真实 gemini handoff 摘要成功（`verify_local_compaction`：本地 budget 到达上限主动压缩 pre_turn × 2、success=True、removed 4/3 条；这也佐证了「到达配置上限即主动压缩、不依赖 provider overflow」的常态路径）。
