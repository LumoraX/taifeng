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
| [tool-builtins-extended](tool-builtins-extended.md) | `apply_patch` atomicity, `BackgroundTaskRegistry`, `http_request`, and builtin `parallel_safe` behavior |
| [mcp-server](mcp-server.md) | `McpStdioServer`, MCP handshake, tools, resources, bidirectional JSON-RPC elicitation, and CLI `mcp serve` |
| [telemetry-otel](telemetry-otel.md) | `OtelSinkConfig`, `OtelTelemetrySink`, EventMsg-to-OTel mapping, PII filtering, counters, and fire-and-forget export |
| [turn-rewind](turn-rewind.md) | Addressable intra-turn nodes, `Rewind` op, `RewindCheckpoint`, `rewind_nodes()`, event classes, rejection paths, R2 expectations, and R5 append-only behavior |
| [detached-spawn](detached-spawn.md) | Detached spawn, join barriers, independent child HITL, keepalive refcounts, `kill_spawn`, cold recovery rebuild, spawn/join events, and LLM-facing tools |
| [reactive-compaction-recovery](reactive-compaction-recovery.md) | Bounded overflow recovery, forced compression, provider retry events, fallback behavior, cache awareness, and cancellation constraints |
| [compaction-surgical-trim](compaction-surgical-trim.md) | Surgical trim passes, pair-safe output rewriting, cache-TTL triggers, glob deny precedence, `CompressionResult.detail`, and idempotent placeholders |
| [compaction-offload-strategy](compaction-offload-strategy.md) | Lossless offload of oversized tool results to disk with stub pointer, deterministic path derivation, `file_read` paged recall (LLM-driven, no auto-rehydrate), idempotent placeholder guard, R2 tail-only / R5 resume, thread-cascade cleanup |
| [turn-resource-guards](turn-resource-guards.md) | `DenialBreaker`, `IterationBudget`, child budget derivation, `ToolSpec.refunds_iteration`, and single-point accounting |
| [postcompact-state-reinjection](postcompact-state-reinjection.md) | `PinnedStateSource`, pinned registry, budgeted reinjection, `system_injection(source=\"pinned:<name>\")`, events, and runtime register/unregister |
| [budget-awareness](budget-awareness.md) | Pre-turn neutral budget-fact injection on `soft_limit` crossing, one-shot-per-crossing, `system_injection(source="budget_hint")`, `budget_hint_injected` event (ADR 0017 rule ②) |
| [peer-mailbox-messaging](peer-mailbox-messaging.md) | Live peer messaging by thread/handle/parent address, queue-only and trigger-turn semantics, `wait_peer` / `wait_any` (any-of-N), `SendToPeer`, and peer events |
| [midturn-input-steering](midturn-input-steering.md) | `InjectUserInput`, pending input queues, iteration-boundary draining, no-active-turn fallback, delivered events, pairing protection, and cancellation guards |
| [audit-observability](audit-observability.md) | 全局 `seq` + per-subscriber `delivery_seq`（`DeliveredEvent`）自检、事件队列有界大容量 + 高/低水位告警迟滞、`enable_request_capture` 下 `LlmRequestRecorded` 全文留痕、OtelSink 按 kind 跳过 |

### Persistence

Aligned with [conversation.md](../conversation.md).

| Contract | Coverage |
| --- | --- |
| [session-journal-business-integration](session-journal-business-integration.md) | Experimental strict runtime slice for new Sessions: Journal-first submissions, LLM/Tool/call_skill intent and outcome, durable conversation items, and per-Session fail-closed gating |
| [session-journal-core](session-journal-core.md) | Phase 1 experimental durable core: canonical envelope/hash chain, atomic JSONL batch frames, live single-writer fencing, strict verification |
| [jsonl-transcript](jsonl-transcript.md) | `MessageWriter`, metadata line, POSIX atomic append, corrupt-line tolerance, `resume_thread_id`, `initial_history`, and `thread_resumed` events |
| [thread-directory](thread-directory.md) | `ThreadMetadata`, `ThreadFilter`, `ThreadPage`, SQLite self-healing, `NullThreadDirectory`, and directory error classes |
| [index-hook](index-hook.md) | `IndexHook`, fire-and-forget timing, exception isolation, shutdown grace period, and index hook failure/abandon events |

### LLM Client

Aligned with [llm-client.md](../llm-client.md).

| Contract | Coverage |
| --- | --- |
| [llm-provider-native](llm-provider-native.md) | Native provider contract, `ResponseEvent` stream shape, Anthropic / Gemini / DeepSeek field mapping, cache field priority, error classification, and `record_cache_read` |
| [llm-codex-provider](llm-codex-provider.md) | 独立 `codex-responses-v1` provider：顶层 instructions、有序 typed input、done-item 终态、provider-state 隔离、恢复与脱敏边界 |
| [llm-structured-output](llm-structured-output.md) | `ResponseFormatSpec`, `structured_output` events, provider translation, and parse failure strategy |
| [llm-sim-conformance](llm-sim-conformance.md) | Stateful conformance simulator, protocol checks, token accounting, prefix-cache ledger, full-fidelity chunks, fault injection, deterministic timing, and request ledger |

### Engineering Conventions

| Contract | Coverage |
| --- | --- |
| [test-layout](test-layout.md) | Test directory organization: subdirectories mirror `src` modules |

---

Historical note: these contracts were originally produced through a spec-driven workflow and promoted into this directory as the stable living contract layer. The repository no longer carries those process artifacts.
