# Taifeng Capability Matrix

> **This document answers one question: what has Taifeng implemented, how do I integrate it, and where is the authoritative contract?**
>
> It is written for teams integrating Taifeng into a host system. A quick scan should tell you whether a capability exists, whether it has landed, which API or tool is the entry point, and where to find examples and contracts.

Documentation layers:

- **This document**: capability inventory, status, entry points, examples, and contract links.
- [`usage.md`](usage.md): installation, usage levels, and code skeletons.
- [`configurable-knobs.md`](configurable-knobs.md): field-level list of construction-time and runtime configuration.
- [`architecture/capabilities/`](architecture/capabilities/README.md): stable field-level contracts for data structures, protocols, events, and constraints.

## Status Legend

| Marker | Meaning |
| --- | --- |
| ✅ | Implemented, covered by tests and runnable examples, ready for production integration |
| 🧪 | Implemented, primarily validated with simulator/mock coverage; real-world limits are documented in the contract |

As of this branch, every capability below has landed as ✅ or 🧪. Feature-gap progress is tracked in [`architecture/hermes-gap-roadmap.md`](architecture/hermes-gap-roadmap.md), and kernel primitive progress is tracked in [`architecture/kernel-gap-analysis.md`](architecture/kernel-gap-analysis.md).

---

## 1. Skill System

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **Skill = markdown** | A skill is a `SKILL.md` document, not a function tool; the LLM is the scheduler | `FilesystemSkillRegistry.load(dir)` / SKILL.md frontmatter | [basic/minimal_chat.py](../examples/basic/minimal_chat.py) | [skill-system.md](architecture/skill-system.md) | [`composite_dispatch`](real-llm-ledger.md) |
| **Lazy `read_skill`** | Child skill lists expose only id + description; the LLM pulls bodies on demand for cache-friendly prompts | `read_skill` tool | [read_skill_lazy/](../examples/read_skill_lazy/) | [skill-system.md](architecture/skill-system.md) | [`read_skill_lazy`](real-llm-ledger.md) |
| **`call_skill` dispatch** | Composite skills recursively dispatch child skills as independent child turns, guarded by depth and cycle checks | `call_skill` tool / `DispatchPolicy` | [basic/composite_skill.py](../examples/basic/composite_skill.py) | [skill-dispatch.md](architecture/capabilities/skill-dispatch.md) ✅ | [`composite_dispatch`](real-llm-ledger.md) |
| **Declarative orchestration** | `orchestration:` in SKILL.md declares `parallel`, `serial`, and `when` plans that run deterministically without LLM sampling | SKILL.md `orchestration` field | [orchestration/demo.py](../examples/orchestration/demo.py) | [skill-orchestration.md](architecture/capabilities/skill-orchestration.md) ✅ | [`orchestration`](real-llm-ledger.md) |
| **Concurrent fan-out** | The LLM can issue multiple `call_skill` calls in one message; with `max_parallel_tool_calls > 1`, they run concurrently | `max_parallel_tool_calls` | [concurrent_fanout/](../examples/concurrent_fanout/) | [skill-dispatch.md](architecture/capabilities/skill-dispatch.md) ✅ | [`concurrent_fanout`](real-llm-ledger.md) |
| **Detached spawn + join barrier** | `spawn_skill` returns a handle immediately; child threads run independently, can suspend for HITL, and are aggregated by `await_skills` / join barriers | `spawn_skill` / `await_skills` / `join_skill` / `kill_skill` tools | [multi_expert_consult/](../examples/multi_expert_consult/) | [detached-spawn.md](architecture/capabilities/detached-spawn.md) ✅ | [`spawn_join`](real-llm-ledger.md) |
| **Nested specialist HITL resume** | Spawned composite specialists can suspend inside child skills and later resume through the `resume_spawn_nested` chain | `Resume(thread_id=<spawn child thread>)` | [nested_hitl_demo.py](../examples/multi_expert_consult/nested_hitl_demo.py) | [detached-spawn.md](architecture/capabilities/detached-spawn.md) ✅ | — |
| **SKILL.md script runtime** | Scripts declared in SKILL.md run through `run_script`, behind both permission and hook gates | `script_executors=` + `run_script` tool | [basic/skill_with_script.py](../examples/basic/skill_with_script.py) | [script-execution.md](architecture/capabilities/script-execution.md) ✅ | [`numeric_loop`](real-llm-ledger.md) |
| **SKILL.md hot reload** | Skill files can be watched so updates take effect without restarting the process | `auto_watch_skills=True` | — | [skill-system.md](architecture/skill-system.md) | — |
| **Runtime eligibility** | Composite child skills become visible according to `requires` / `exposure` and host-provided `RuntimeCapabilities` | SKILL.md `requires` / `exposure` | [subagent_isolation/](../examples/subagent_isolation/) | [skill-system.md](architecture/skill-system.md) | — |

## 2. Agent Loop and Tools

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **Turn loop** | Submission/EventMsg dual-bus actor; each turn runs in its own task and does not block the engine loop | `AgentEngine` / `engine.submit` / `engine.subscribe` | [basic/minimal_chat.py](../examples/basic/minimal_chat.py) | [agent-loop.md](architecture/agent-loop.md) | [`composite_dispatch`](real-llm-ledger.md) |
| **Multi-engine reuse + resume** | One process can cache engines for many sessions; persisted threads resume by `resume_thread_id` | `EnginePool.create` / `get_or_create(resume_thread_id=)` | [persistence/](../examples/persistence/) | [agent-loop.md](architecture/agent-loop.md) | [`suspend_resume`](real-llm-ledger.md) |
| **Turn cancellation** | Cooperative cancellation with parent-child token propagation | `CancelTurn` op / `CancellationToken` | [web_ui/](../examples/web_ui/) | [agent-loop.md](architecture/agent-loop.md) | — |
| **Mid-turn input steering** | User input can be injected into a running turn and drained at the next iteration boundary without stopping the turn | `InjectUserInput` op / `user_input_injected` event | [web_ui/](../examples/web_ui/) | [midturn-input-steering.md](architecture/capabilities/midturn-input-steering.md) ✅ | — |
| **System message injection** | Host systems can inject system notes between turns without affecting the active turn | `InjectSystemMessage` op | — | [agent-loop.md](architecture/agent-loop.md) | — |
| **Turn rewind** | Root turns are indexed into addressable nodes so sampling loops or `call_skill` points can be replayed | `Rewind` op (`re_reason` / `retry_tool`) | [turn_rewind/](../examples/turn_rewind/) | [turn-rewind.md](architecture/capabilities/turn-rewind.md) ✅ | [`turn_rewind`](real-llm-ledger.md) |
| **Thread-addressable rewind** | Failed spawn child threads can be truncated and replayed from a failed step while preserving prior steps and answered HITL records | `Rewind(thread_id=child_tid)` / `engine.rewind_nodes_for(tid)` | — | [turn-rewind.md](architecture/capabilities/turn-rewind.md) §thread addressing ✅ + ADR 0018 | [`thread_rewind`](real-llm-ledger.md) |
| **HITL suspend / cross-instance resume** | User input collection can suspend, release the live instance, rebuild across processes, and resume with `Resume` | `request_user_input` tool / `Resume` op / `SuspensionResolver` | [suspend_resume/](../examples/suspend_resume/) | [suspend-resume.md](architecture/capabilities/suspend-resume.md) ✅ | [`suspend_resume`](real-llm-ledger.md) |
| **HITL permission approval** | Action-level permission requests for skill dispatch, scripts, and network access, with typed payloads and timeout support | `permission_policy=` / `PermissionPolicy` / `CallbackPrompter` | [permission/](../examples/permission/) · [selective_approval/](../examples/selective_approval/) | [permission-gate.md](architecture/capabilities/permission-gate.md) ✅ | [`selective_approval`](real-llm-ledger.md) |
| **Reusable approval grants** | Scoped, deterministic-lifecycle grants that pre-answer matching `ask` requests (bypass the prompt, never override `deny`); tree-wide by default with subtree narrowing | `PermissionPolicy.issue_grant` / `revoke_grant` / `PermissionGrant` | — | [permission-gate.md](architecture/capabilities/permission-gate.md#requirement-可复用审批-grantpermission-grants) ✅ | — (sim：`tests/permission/test_grant*.py`；策略层机制，不依赖模型行为) |
| **Hooks** | 8 hook families intercept tool use, skill dispatch, script use, turns, and compaction | `hooks=` / `HookRunner` | [hooks_showcase/](../examples/hooks_showcase/) | [hooks.md](architecture/capabilities/hooks.md) ✅ | — |
| **Layered instructions + hot reload** | Engine/session/turn instruction scopes inject system guidance through a protocolized resolver | `instruction_layers=` / `UpdateInstructions` op / `InstructionSource` | [basic/instructions_basic.py](../examples/basic/instructions_basic.py) | [instructions-injection.md](architecture/capabilities/instructions-injection.md) ✅ | — |
| **Tool whitelist consistency** | Visible tools have one source of truth; scripts are folded into `run_script`, and dispatch rejects `not_offered` calls | `SkillDefinition.visible_tool_names()` / `dispatch_batch(visible_tools=)` | [travel_planner/](../examples/travel_planner/) | [tool-whitelist.md](architecture/capabilities/tool-whitelist.md) ✅ | [`travel_planner`](real-llm-ledger.md) |
| **Builtin tool set** | Skill IO, file IO, shell, patching, HTTP, background tasks, script execution, HITL input, spawn tools, peer messaging, and todo state | `extra_tools=` / `make_*_tool()` | [numeric_loop/](../examples/numeric_loop/) | [tool-builtins-extended.md](architecture/capabilities/tool-builtins-extended.md) ✅ | [`numeric_loop`](real-llm-ledger.md) |
| **Background tasks** | The LLM can start long-running background work and poll for completion | `run_in_background` / `wait_for_task` tools / `BackgroundTaskRegistry` | — | [tool-builtins-extended.md](architecture/capabilities/tool-builtins-extended.md) ✅ | — |

## 3. Context Compression

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **Local budget-triggered compaction** | Compression triggers when configured `context_window` limits are reached, without waiting for provider overflow errors | `budget=ContextBudget(...)` / `compressors=` | [compression_showcase/demo.py](../examples/compression_showcase/demo.py) | [context-compression.md](architecture/context-compression.md) | [`compression`](real-llm-ledger.md) |
| **Handoff compaction** | Codex-style LLM-to-LLM handoff summary with quality audit and health rollback | `HandoffCompactionStrategy` | [overflow_demo.py](../examples/compression_showcase/overflow_demo.py) · [capability_matrix.py](../examples/real_llm/capability_matrix.py) | [context-compression.md](architecture/context-compression.md) | — |
| **Sliding-window compaction** | Keeps the last N items plus image markers | `SlidingWindowStrategy` | [compression_showcase/demo.py](../examples/compression_showcase/demo.py) | [context-compression.md](architecture/context-compression.md) | [`compression`](real-llm-ledger.md) |
| **Bounded overflow recovery** | Provider overflow triggers one forced compaction plus one retry instead of immediately failing the turn | automatic when `compressors` are configured / `provider_retry` event | [overflow_demo.py](../examples/compression_showcase/overflow_demo.py) | [reactive-compaction-recovery.md](architecture/capabilities/reactive-compaction-recovery.md) 🧪 | — |
| **Manual compaction** | Host systems can request compaction explicitly | `CompactNow` op | — | [context-compression.md](architecture/context-compression.md) | — |
| **Cache-friendly contract** | Every compaction reports `cache_invalidated` and `anchor_preserved_until`; mid-turn compaction touches only the tail | `CompressionResult` / `cache_break_detected` event | [observability/](../examples/observability/) | [context-compression.md](architecture/context-compression.md) | — |
| **Runtime budget updates** | Context budgets can be changed between turns | `UpdateBudget` op | — | [configurable-knobs.md](configurable-knobs.md) | — |
| **Budget awareness** | Pre-turn injects a neutral budget fact (used %, tokens until hard limit) when usage crosses `soft_limit`, once per crossing, so the model can self-converge (ADR 0017 rule ②) | automatic on `soft_limit` crossing / `budget_hint_injected` event | [compression_showcase/demo.py](../examples/compression_showcase/demo.py) | [budget-awareness.md](architecture/capabilities/budget-awareness.md) 🧪 | — |

## 4. LLM Client

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **Native providers + LiteLLM fallback** | OpenAI-compatible, Anthropic, Gemini, and DeepSeek native HTTP clients, plus LiteLLM for broader coverage | `model_client=` | [real_llm/e2e.py](../examples/real_llm/e2e.py) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ | [`composite_dispatch`](real-llm-ledger.md) |
| **Unified `ResponseEvent` stream** | 11 event kinds normalize text, tool calls, reasoning, prompt cache, rate limits, structured output, and more | `ModelClient` protocol / `ResponseEvent` | [observability/](../examples/observability/) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ | [`composite_dispatch`](real-llm-ledger.md) |
| **Structured output** | Strongly typed output schemas are translated into provider-specific request shapes | `ResponseFormatSpec` / `structured_output` event | — | [llm-structured-output.md](architecture/capabilities/llm-structured-output.md) ✅ | — |
| **Retry + error classification** | Exponential backoff, server hint delays, 11 error classes, and machine-readable recovery plans | `retry_async` / `LLMError` / `RecoveryPlan` | — | [llm-client.md](architecture/llm-client.md) | — |
| **Prompt-cache accounting** | Provider-specific cache counters are read directly and normalized into `PromptCacheStats` / `TokenUsage` | `PromptCacheStats` / `TokenUsage` | [real_llm/e2e.py](../examples/real_llm/e2e.py) | [llm-provider-native.md](architecture/capabilities/llm-provider-native.md) ✅ | — |
| **Conformance simulator** | Stateful simulator validates protocol shape, token overflow, prefix cache, fault injection, deterministic timing, and request ledgers | `SimClient` / `RoutingSimClient` / `SimTurn` / `sim_client` fixture | [tests/llm/test_sim_engine_integration.py](../tests/llm/test_sim_engine_integration.py) | [llm-sim-conformance.md](architecture/capabilities/llm-sim-conformance.md) ✅ | CI simulator regression |
| **Golden calibration** | Real stream shapes are recorded as redacted fixtures; drift becomes a red test and re-recording requires review | `extract_shape` / `--record` / `tests/llm/golden/` | [tests/llm/test_golden_calibration.py](../tests/llm/test_golden_calibration.py) | [llm-sim-conformance.md](architecture/capabilities/llm-sim-conformance.md) §golden calibration ✅ | Golden fixtures derive from the real regression ledger |

## 5. Persistence

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **Append-only JSONL store** | One file per thread, metadata first line, POSIX atomic append, corrupt-line tolerance, and source-of-truth storage | `JsonlMessageStore` / `MessageWriter` protocol | [persistence/](../examples/persistence/) | [jsonl-transcript.md](architecture/capabilities/jsonl-transcript.md) ✅ | [`suspend_resume`](real-llm-ledger.md) |
| **SQLite side index** | Zero-config stdlib SQLite + WAL index that can rebuild itself from JSONL | default / `rebuild_index()` | [persistence/](../examples/persistence/) | [thread-directory.md](architecture/capabilities/thread-directory.md) ✅ | — |
| **Pluggable directory backends** | Metadata and list queries can use host backends while JSONL remains the primary store | `thread_directory=` / `ThreadDirectory` protocol | [redis_thread_directory.py](../examples/persistence/redis_thread_directory.py) · [postgres_thread_directory.py](../examples/persistence/postgres_thread_directory.py) | [thread-directory.md](architecture/capabilities/thread-directory.md) ✅ | — |
| **Audit / ES / Kafka projection** | Thread lifecycle events can be projected fire-and-forget to external indexes | `index_hook=` / `IndexHook` protocol | [audit_index_hook.py](../examples/observability/audit_index_hook.py) | [index-hook.md](architecture/capabilities/index-hook.md) ✅ | — |
| **History rollback** | A thread can be truncated before a selected item | `ThreadRollback` op | — | [conversation.md](architecture/conversation.md) | — |

## 6. Observability and Integration

| Capability | Summary | Entry Point | Example | Contract | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| **EventMsg bus** | Critical runtime paths emit events; callers can subscribe per submission or globally | `engine.subscribe` / `subscribe_all` | [web_ui/](../examples/web_ui/) | [agent-loop.md](architecture/agent-loop.md) | [`composite_dispatch`](real-llm-ledger.md) |
| **Audit observability (层1)** | 全局 `seq` + per-subscriber `delivery_seq`（`DeliveredEvent`）丢失自检、事件队列有界大容量（默认 65536，不 OOM）+ 高/低水位告警迟滞、`enable_request_capture` 下 `LlmRequestRecorded` 全文留痕（OtelSink 按 kind 跳过） | `enable_request_capture=` / `event_queue_size=` / `subscribe_all_envelopes` / `engine.session_id` | [audit_observability/](../examples/audit_observability/demo.py) | [audit-observability.md](architecture/capabilities/audit-observability.md) ✅ | — |
| **Console / JSONL sinks** | Human-readable and machine-readable telemetry sinks are available out of the box | `attach_console_sink` / `attach_jsonl_sink` | [observability/](../examples/observability/) | [agent-loop.md](architecture/agent-loop.md) | [`composite_dispatch`](real-llm-ledger.md) |
| **OpenTelemetry sink** | EventMsg to OTLP export, PII filtering, nested spans, and prebuilt counters | `OtelTelemetrySink` / `OtelSinkConfig` (`[telemetry-otel]` extra) | — | [telemetry-otel.md](architecture/capabilities/telemetry-otel.md) ✅ | — |
| **Taifeng as MCP server** | Exposes skill turns as MCP tools for Claude Code / Cursor and supports bidirectional elicitation | `McpStdioServer` / CLI `mcp serve` | [mcp_basic/](../examples/mcp_basic/) · [mcp_showcase/](../examples/mcp_showcase/) | [mcp-server.md](architecture/capabilities/mcp-server.md) ✅ | — |
| **Taifeng as MCP client** | Connects to external MCP servers and auto-registers their tools | `McpStdioClient` | [mcp_basic/](../examples/mcp_basic/) · [mcp_hitl/](../examples/mcp_hitl/) | [mcp-server.md](architecture/capabilities/mcp-server.md) ✅ | — |
| **Web realtime panel** | FastAPI + SSE reference implementation for streaming agent data, demo switching, permission UI, and resume | [web_ui/server.py](../examples/web_ui/) | [web_ui/](../examples/web_ui/) | — | — |

## 7. Kernel Resource Knobs

The mechanisms live in the kernel; policy values and external backends are injected by host systems to preserve R1. Safe defaults exist even without host injection. See [configurable-knobs.md §1.0](configurable-knobs.md).

| Knob | Kernel Dimension | Summary | Entry Point | Example | Real LLM Validation |
| --- | --- | --- | --- | --- | --- |
| `max_concurrent_spawns` / `max_total_spawns` | K1 breadth admission | Limits in-flight spawn breadth to prevent fork bombs; HITL-suspended children do not consume running slots | `EnginePool.create(max_concurrent_spawns=)` | [kernel_knobs/](../examples/kernel_knobs/) | [`kernel_knobs`](real-llm-ledger.md) |
| `max_session_tokens` | K2 resource enforcement | Session-wide token hard ceiling that rejects new turns or stops sampling when reached | `EnginePool.create(max_session_tokens=)` | [kernel_knobs/](../examples/kernel_knobs/) | [`kernel_knobs`](real-llm-ledger.md) |
| `memory_store` | K3 memory hierarchy | Long-term memory swap/page-fault surface with host-provided backend hooks | `memory_store=` / `MemoryStore` protocol | [memory/](../examples/memory/) | — |
| `submission_queue_size` / `event_queue_size` | K4 flow control | Inbound backpressure and outbound event queue sizing | `EnginePool.create(...)` | [kernel_knobs/](../examples/kernel_knobs/) | — |

---

## Topic: Multi-Track Concurrency Observability

Typical case: a multi-expert consultation skill spawns several experts in one turn. Each expert runs independently, can suspend for HITL at a different time, and is aggregated through a join barrier. Frontends need separate tracks for each expert's streamed output, tool calls, and HITL state.

The kernel already emits everything required. The track key is **`EventMsg.submission_id`**.

| Track | Event `submission_id` | Mapping Source |
| --- | --- | --- |
| Orchestration entry turn | The `sub_id` returned by the initial user submission | `engine.submit(UserMessage)` |
| Each concurrent expert child | The expert's `child_thread_id` | `spawn_started.data = {handle_id, skill_id, child_thread_id}` |
| Final consultation aggregation turn | The `then_thread_id` | `join_barrier_fired.data = {barrier_id, then_thread_id}` |

Why this works:

- `_build_child_runner` constructs each child runner with `submission_id=child_thread_id` ([`src/taifeng/loop/engine.py`](../src/taifeng/loop/engine.py)).
- Every event is wrapped as `EventMsg(submission_id=self.submission_id, ...)` ([`src/taifeng/loop/turn.py`](../src/taifeng/loop/turn.py)).
- Therefore `turn_started`, `assistant_text`, `tool_call_started`, `tool_call_completed`, and `skill_dispatched` events naturally belong to the correct concurrent track.

Frontend projection sketch:

```python
tracks = {}
async for ev in engine.subscribe_all():
    m, sid = ev.msg, ev.submission_id
    if m.kind == "spawn_started":
        tracks[m.data["child_thread_id"]] = {"skill": m.data["skill_id"], "state": "running"}
    elif m.kind == "spawn_suspended":
        tracks[m.data["thread_id"]]["state"] = "hitl_waiting"
    elif m.kind == "join_barrier_fired":
        tracks[m.data["then_thread_id"]] = {"skill": "consultation", "state": "running"}
    elif m.kind in ("assistant_text", "tool_call_started", "tool_call_completed"):
        tracks.setdefault(sid, {"skill": "?", "state": "running"})
        render_into_track(sid, m)
```

Spawn/join lifecycle events are `spawn_started`, `spawn_suspended`, `spawn_completed`, `spawn_failed`, `spawn_cancelled`, `join_barrier_registered`, and `join_barrier_fired`. See [multi_expert_consult/](../examples/multi_expert_consult/), [web_ui/](../examples/web_ui/), and [detached-spawn.md](architecture/capabilities/detached-spawn.md).

Host SSE layers may rename `submission_id` to `track_id`; the kernel stays unaware of host UI semantics.

---

## Integration Entry Points

### `EnginePool.create`

The full signature is in [`src/taifeng/loop/pool.py`](../src/taifeng/loop/pool.py). Field-level documentation is in [configurable-knobs.md §1](configurable-knobs.md). Minimal construction:

```python
pool = await taifeng.EnginePool.create(
    skills_dir="./skills",
    storage_dir="./data",
    model_client=client,
)
```

### Runtime Ops

Runtime ops are submitted with `engine.submit(...)`.

| Op | Purpose |
| --- | --- |
| `UserMessage` | Start or continue a turn |
| `InjectUserInput` | Inject incremental user input into a running turn |
| `InjectSystemMessage` | Add a system note without affecting the active turn |
| `CancelTurn` | Cancel a running turn |
| `CompactNow` | Trigger compaction manually |
| `Resume` | Continue after HITL suspension |
| `Rewind` | Re-run from an addressable node |
| `SendToPeer` | Send a lineage-scoped peer message |
| `ThreadRollback` | Truncate persisted history |
| `UpdateBudget` | Change the context budget |
| `UpdateInstructions` | Hot-reload an instruction layer |
| `RefreshSnapshot` | Refresh the skill snapshot |
| `Shutdown` | Shut down an engine |

### Builtin Tools

`read_skill` · `call_skill` · `file_read` · `file_write` · `shell_exec` · `apply_patch` · `http_request` · `run_in_background` · `wait_for_task` · `run_script` · `request_user_input` · `spawn_skill` · `await_skills` · `join_skill` · `kill_skill` · `send_message` · `wait_peer` · `todo_write`

### Public API Symbols

All exported symbols are listed in [`src/taifeng/__init__.py`](../src/taifeng/__init__.py) under `__all__`.

---

## Verification Status

- **Full regression**: `PYTHONPATH=src uv run pytest tests/`. CI uses the conformance simulator and does not call real APIs.
- **Real LLM regression**: [`examples/real_llm/capability_matrix.py`](../examples/real_llm/capability_matrix.py) runs high-risk scenarios such as suspend/resume, rewind, spawn/join, peer messaging, and kernel knobs with real provider keys, then updates [`real-llm-ledger.md`](real-llm-ledger.md).
- **Merge red line**: changes under `src/taifeng/{llm,loop,context,conversation}/` require a full real-LLM capability matrix run and a committed ledger update.
- **Before burning keys**: [`examples/real_llm/selfcheck.py`](../examples/real_llm/selfcheck.py) runs the driver orchestration against the simulator.
- Capability boundaries and limitations are recorded in the matching contract documents.
