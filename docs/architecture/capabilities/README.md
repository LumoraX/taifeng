# Capability Contracts

> This directory contains Taifeng's stable capability contracts: data structures, protocol signatures, behavior constraints, event categories, and enum values.
>
> Architecture narrative documents in `docs/architecture/*.md` explain how modules collaborate. This directory defines each capability precisely. Read the overview and module documents for system shape; read these contracts for field-level details.

Each contract follows an EARS-style structure: `Requirement`, `Scenario`, data contract, and behavior contract.

## Index by Module Area

### Skill System

Aligned with [skill-system.md](../skill-system.md).

| Contract | Coverage |
| --- | --- |
| [skill-dispatch](skill-dispatch.md) | `call_skill` lifecycle, Permission + Hook gates, `subagent_approval_mode`, `_SubagentAutoDecisionPolicy`, `reason` propagation, CallStack, and DispatchVerdict contracts |
| [skill-orchestration](skill-orchestration.md) | Declarative orchestration (`parallel` / `serial` / `when`), load-time validation, deterministic execution without LLM sampling, and `orchestration_plan_resolved` events |
| [skill-outcome-record](skill-outcome-record.md) | `SkillExecutionRecord` 数据契约（全字段含义）、`OutcomeJudge` 协议 + `StructuralOutcomeJudge` 默认映射、长相/战绩分离不变量、旁路 `skill_outcome` ItemKind、`skill_outcome_recorded` 事件、终态触发规则（suspended 不记）与 v1 显式边界 |
| [skill-outcome-record § 战绩聚合](skill-outcome-record.md) | 战绩聚合：`SkillFitnessStore` 协议、`SkillFitnessRecorder`（TelemetrySink 适配）、`InMemorySkillFitnessStore`（实验层，只沉淀不决策，ADR 0067）|
| [skill-authorization](skill-authorization.md) | 白名单外 skill 的派发授权：`SkillAuthorizationPolicy`（`discoverable` / `authorize`）、`CallbackSkillAuthorization` / `PermissionSkillAuthorization`（`skill_authorization` 权限范围）、`DispatchPolicy.authorization`；召回池并入白名单外可发现的 skill（`requires_authorization`）、`call_skill` 授权阶段、`skill_authorization_granted` / `skill_authorization_denied` 事件（实验层，opt-in，ADR 0089）|
| [skill-selection-gate](skill-selection-gate.md) | 按选择置信度分流：`SelectionConfidencePolicy` / `ThresholdSelectionPolicy`（proceed / trial / escalate）、`TrialJudge` / `VerifierTrialJudge`、`SkillSelectionGate`；`search_skills` 结果标注 `route`、`call_skill` / `spawn_skill` 派发门、`skill_selection_routed` / `skill_selection_gated` 事件（实验层，opt-in，ADR 0088）|
| [skill-working-set](skill-working-set.md) | 按战绩算分与工作集规划：`FitnessScorer` / `WilsonFitnessScorer`、`WorkingSetPolicy` / `TierRule` / `plan_working_set`（提拔 / 逐出 / 隔离 / 解除，无状态重算）、`SkillFitnessShadow` 影子评估、`SkillWorkingSet` 生效（turn 级快照、`quarantine_effect`、`skill_promoted` / `skill_evicted` / `skill_quarantined` / `skill_released`）、来源信任分层 `SkillTrustPolicy` / `SourceTrustPolicy`（实验层，opt-in，ADR 0077 / 0090）|
| [skill-recall](skill-recall.md) | skill 发现/召回/验证：`SkillCandidate` / `RecallEntry` / `SkillRecall` 协议 + `KeywordSkillRecall`（BM25-lite）/ `LlmSkillRecall`；opt-in 自动发现总闸 `enable_auto_discovery`；召回后验证门 `SkillVerifier` / `VerifiedCandidate` / `LlmSkillVerifier`（拉完整 body 判输入要求适配、长相 vs 适配分字段、C2 护栏）/ `SkillVerifyParseError`；deferred 暴露判定（`effective_child_recall` 单一真相 + `recall_threshold`）、召回作用域=白名单内 G4 过滤、`search_skills` payload + 置信路由（no_match 显式信号）、`skill_search_invoked` / `skill_candidates_returned` / `skill_candidates_verified` 事件、选择溯源连回 v1（`discovered`，用 verify_confidence） |

### Agent Loop, Tools, and Infrastructure

Aligned with [agent-loop.md](../agent-loop.md).

| Contract | Coverage |
| --- | --- |
| [hooks](hooks.md) | 8 HookKind values, hook payload fields, call-site mapping, `PreTurnHookDenied`, and `PreCompactHookSkipped` events |
| [instructions-injection](instructions-injection.md) | `InstructionSource` protocol, three scope cache levels, hot reload semantics, fail-fast behavior, and 5 event classes |
| [permission-gate](permission-gate.md) | `PermissionRequest`, factory methods, `prompter_timeout_seconds`, tri-state `args_match`, `PermissionRule.parse` / `from_dict`, stateless kernel constraints, and reusable approval grants (`PermissionGrant` / `GrantStore`, ADR 0022) |
| [suspend-resume](suspend-resume.md) | `SuspendReason`, `PendingRequest`, `SuspensionRecord`, `Resume` op, `ResolvePlan`, `SuspensionResolver`, resume semantics, idempotent resolved markers, tier-1/2 recovery, and cross-process rebuild |
| [script-execution](script-execution.md) | `ScriptDescriptor` / `ScriptExecutor`, implicit discovery, subprocess isolation, timeout / cancellation, and script events |
| [tool-whitelist](tool-whitelist.md) | Single source of truth for visible tools, `run_script` integration, dispatch-side `not_offered` checks, and replay exemptions |
| [tool-argument-validation](tool-argument-validation.md) | 派发前按 `input_schema` 校验参数（内置子集校验器，不认识的关键字放过）、违例 + schema 回显的改参反馈、`arguments_rejection` 全路径单一入口（ADR 0047） |
| [tool-builtins-extended](tool-builtins-extended.md) | `apply_patch` atomicity, `BackgroundTaskRegistry`, `http_request`, opt-in `glob` / `grep` search (context lines, multiline, `.gitignore`) and `memory` tool with optional `ForgettableMemoryStore` delete (ADR 0064 / 0071), and builtin `parallel_safe` behavior |
| [tool-image-attachment](tool-image-attachment.md) | 工具返回图片附件：`ToolResult.attachments`、落盘前 admission、`tool_output_modalities` 协议分档、fco 内 `PartContent` 投影与 wire `input_image`、`requires.modalities` 路由期门控、能力不足时 in-band 降级，以及内核/业务边界 |
| [dynamic-tool-set](dynamic-tool-set.md) | `ToolRegistry.unregister` / `replace` / `version` / `subscribe`、`tool_set_changed` 事件、prompt 指纹含描述与 schema、MCP `McpClient` 协议 + `bind_mcp_tools` 随 `tools/list_changed` 同步、`McpHttpClient` streamable HTTP 传输（ADR 0048） |
| [mcp-client](mcp-client.md) | taifeng 作为 MCP 客户端：2025-06-18 版本协商（清单外断开、HTTP `MCP-Protocol-Version` 头）、`tools/list` 跟完分页（页数上限）、tools/call 结果无损投影（图片走附件 admission、structuredContent 进 `data`、resource / audio 显式标注）与 outputSchema 校验、放弃请求时发 `notifications/cancelled`、HTTP 断流按 `Last-Event-ID` 续传、server → client 请求路由（`ping` / `elicitation/create` / `-32601` / `notifications/cancelled`）与 `ElicitationHandler` 注入口（ADR 0063 / 0069） |
| [mcp-server](mcp-server.md) | `McpStdioServer`, MCP handshake（版本协商、记录客户端能力）, tools, resources, bidirectional JSON-RPC elicitation（只向声明了 `elicitation` 能力的客户端发）, and CLI `mcp serve` |
| [telemetry-otel](telemetry-otel.md) | `OtelSinkConfig`, `OtelTelemetrySink`, EventMsg-to-OTel mapping, PII filtering, counters, and fire-and-forget export |
| [turn-rewind](turn-rewind.md) | Addressable intra-turn nodes, `Rewind` op, `RewindCheckpoint`, `rewind_nodes()`, event classes, rejection paths, R2 expectations, and R5 append-only behavior |
| [detached-spawn](detached-spawn.md) | Detached spawn, join barriers, independent child HITL, keepalive refcounts, `kill_spawn`, cold recovery rebuild, spawn/join events, and LLM-facing tools |
| [provider-circuit-breaker](provider-circuit-breaker.md) | 跨 turn 的 provider 健康度：`BreakerConfig` / `CircuitState` / `CircuitTransition`、只计最终结局的计数规则、open 态快速失败（`CircuitOpenError`）、半开单探测、三态事件与 OTel counter，以及作用域与误跳闸边界 |
| [reactive-compaction-recovery](reactive-compaction-recovery.md) | Bounded overflow recovery, forced compression, provider retry events, fallback behavior, cache awareness, and cancellation constraints |
| [compaction-surgical-trim](compaction-surgical-trim.md) | Surgical trim passes, pair-safe output rewriting, cache-TTL triggers, glob deny precedence, `CompressionResult.detail`, and idempotent placeholders |
| [loop-core-module-structure](loop-core-module-structure.md) | engine / turn 的模块边界、协作者按内聚度二分、宿主作为唯一白盒寻址面（含两类 monkeypatch 注入点）、行为零变化判据，以及两处具名红线例外 |
| [cache-anchor](cache-anchor.md) | Inclusive cache anchor semantics, advancement after each successful sample, strategy windows from `anchor+1`, rollback rules, and history→messages breakpoint mapping |
| [compaction-offload-strategy](compaction-offload-strategy.md) | Lossless offload of oversized tool results to disk with stub pointer, deterministic path derivation, `file_read` paged recall (LLM-driven, no auto-rehydrate), idempotent placeholder guard, R2 tail-only / R5 resume, thread-cascade cleanup |
| [compaction-multimodal-eviction](compaction-multimodal-eviction.md) | Eviction of old image / file attachments: payload-only rewrite with a descriptor line (type, size, filename, sha256 prefix), `keep_recent` counted in attachment-bearing items, selective trigger, anchor-aware window, `detail` counts (experimental, ADR 0082) |
| [compaction-growth-baseline](compaction-growth-baseline.md) | Re-compaction gate: baseline stamped on the `compacted` item (`post_compaction_tokens`), `ContextBudget.recompact_min_growth_ratio`, deferral below `baseline × (1 + ratio)` unless the hard limit is reached, `compaction_deferred` event (ADR 0083) |
| [compaction-background](compaction-background.md) | Background deferred compaction: wrapper strategy computes on a history snapshot, applies at the next pre-turn if the prefix is unchanged, synchronous above `urgent_ratio`, per-thread isolation, `aclose` on pool shutdown (experimental, ADR 0087) |
| [failure-recovery-recipes](failure-recovery-recipes.md) | Declarable recovery recipes: `RecoveryRecipeBook` overrides per failure class (validated at construction), `RecoveryRecipeProvider` capability on the failure policy, `custom_steps`, `source: "declared"` in `turn_failed.recovery` (experimental, ADR 0084) |
| [input-origin](input-origin.md) | Input provenance tags on item metadata (`InputOrigin{kind, trust, label}`), declared on Ops and `ToolSpec.output_trust`, inherited by compaction summaries / child-skill seeds / peer messages, summarised as `input_taint` for tools and hooks; never rendered into the prompt (experimental, ADR 0085) |
| [turn-resource-guards](turn-resource-guards.md) | `DenialBreaker`, `IterationBudget`, child budget derivation, `ToolSpec.refunds_iteration`, and single-point accounting |
| [postcompact-state-reinjection](postcompact-state-reinjection.md) | `PinnedStateSource`, pinned registry, budgeted reinjection, `system_injection(source=\"pinned:<name>\")`, events, and runtime register/unregister |
| [token-accounting-calibration](token-accounting-calibration.md) | 跨 provider `TokenUsage.input_tokens` 口径归一（含缓存）、`TokenCalibration` 实测锚点 + 增量粗估三档估算、压缩改写前缀时失效保留 overhead、`ContextBudget.output_reserve_tokens` 与按 entry skill `max_output_tokens` 派生的生效预留（`with_output_reserve` / `TurnRunner.effective_budget`）、全链路单一估算口径（ADR 0043 / 0071） |
| [usage-tree-accounting](usage-tree-accounting.md) | 会话用量整棵 turn 树共享记账：`SessionUsageMeter` 采样即入账、按 skill / thread 归因、K2 读实时总量（子树无法绕过上限）、`turn_completed.subtree_usage` / `thread_id` / `skill_id`、`introspect()["usage"]`（ADR 0044） |
| [tool-crash-reconciliation](tool-crash-reconciliation.md) | 工具执行途中崩溃的冷恢复：Chat 路径派发前落 `tool_intent`、悬空调用识别、按 `effect_kind` / `ToolSpec.reconcile` 分流（可安全重发 / 回查补写 / `TOOL_OUTCOME_UNKNOWN` 交人裁决）、`EnginePool(tool_recovery=)`、`thread_resumed.recovered_tool_calls`（ADR 0045）；strict audit resume 同样分流，结论写成 `tool_recovery_committed`，交人经 `AuditConfig.tool_outcome_resolver`（ADR 0070） |
| [budget-awareness](budget-awareness.md) | Pre-turn neutral budget-fact injection on `soft_limit` crossing, one-shot-per-crossing, `system_injection(source="budget_hint")`, `budget_hint_injected` event (ADR 0017 rule ②) |
| [peer-mailbox-messaging](peer-mailbox-messaging.md) | Live peer messaging by thread/handle/parent address, queue-only and trigger-turn semantics, `wait_peer` / `wait_any` (any-of-N), `SendToPeer`, and peer events |
| [midturn-input-steering](midturn-input-steering.md) | `InjectUserInput`, pending input queues, iteration-boundary draining, no-active-turn fallback, delivered events, pairing protection, and cancellation guards |
| [audit-observability](audit-observability.md) | 全局 `seq` + per-subscriber `delivery_seq`（`DeliveredEvent`）自检、事件队列有界大容量 + 高/低水位告警迟滞、`enable_request_capture` 下 `LlmRequestRecorded` 全文留痕、OtelSink 按 kind 跳过 |

### Persistence

Aligned with [conversation.md](../conversation.md).

| Contract | Coverage |
| --- | --- |
| [session-journal-business-integration](session-journal-business-integration.md) | Experimental strict runtime slice: Journal-first submissions, LLM/Tool/call_skill intent and outcome, durable conversation items, per-Session fail-closed gating, and Journal-takeover resume that fails closed on unsettled effects (ADR 0053) |
| [session-journal-core](session-journal-core.md) | Experimental durable core: canonical envelope/hash chain, atomic JSONL batch frames, cross-process flock writer exclusion, `open_existing` epoch takeover, strict verification incl. monotonic writer epoch |
| [jsonl-transcript](jsonl-transcript.md) | `MessageWriter`, metadata line, POSIX atomic append, corrupt-line tolerance, `resume_thread_id`, `initial_history`, and `thread_resumed` events |
| [thread-directory](thread-directory.md) | `ThreadMetadata`, `ThreadFilter`, `ThreadPage`, SQLite self-healing, `NullThreadDirectory`, and directory error classes |
| [index-hook](index-hook.md) | `IndexHook`, fire-and-forget timing, exception isolation, shutdown grace period, and index hook failure/abandon events |

### LLM Client

Aligned with [llm-client.md](../llm-client.md).

| Contract | Coverage |
| --- | --- |
| [llm-provider-native](llm-provider-native.md) | Native provider contract, `ResponseEvent` stream shape, Anthropic / Gemini / DeepSeek field mapping, cache field priority, error classification, and `record_cache_read` |
| [llm-codex-provider](llm-codex-provider.md) | 独立 `codex-responses-v1` provider：顶层 instructions、有序 typed input、done-item 终态、provider-state 隔离、恢复与脱敏边界 |
| [llm-file-input](llm-file-input.md) | 🧪 用户消息文件（首批 PDF）输入：`FileAttachmentV1` / `FilePart` canonical 形态、`FileInputPolicy` admission 与按页成本估算、`"file"` 能力门控、OpenAI Chat / Responses / Codex / Anthropic / Gemini wire 映射、脱敏与压缩占位（ADR 0068） |
| [model-routing-composition](model-routing-composition.md) | 多模型路由 / 回退包装器的组合契约：叠加顺序（回退在断路器外）、零产出才回退、按 failure_class 决定、能力取交集、协议不混组、可观测与缓存影响；内核不实现路由（ADR 0067） |
| [llm-image-input](llm-image-input.md) | 用户消息图片输入：`ImageAttachmentV1` / `ImagePart` canonical 形态、admission 与成本估算、OpenAI Chat/Responses 与 Codex 协议映射、持久化压缩与脱敏边界 |
| [llm-provider-native § thinking-passback](llm-provider-native.md) | Anthropic thinking / redacted_thinking 块连同签名、Gemini thoughtSignature 的解析 → `reasoning_state` / `extra_content` 落史 → 续传原样回传；`thinking_budget_tokens` / `thinking_budget` / `include_thoughts` 配置与冲突校验（ADR 0046） |
| [llm-structured-output](llm-structured-output.md) | `ResponseFormatSpec`, `structured_output` events, provider translation, and parse failure strategy |
| [llm-sim-conformance](llm-sim-conformance.md) | Stateful conformance simulator, protocol checks, token accounting, prefix-cache ledger, full-fidelity chunks, fault injection, deterministic timing, and request ledger |

### Engineering Conventions

| Contract | Coverage |
| --- | --- |
| [test-layout](test-layout.md) | Test directory organization: subdirectories mirror `src` modules |

---

Historical note: these contracts were originally produced through a spec-driven workflow and promoted into this directory as the stable living contract layer. The repository no longer carries those process artifacts.
