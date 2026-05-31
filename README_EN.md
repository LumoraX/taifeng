<div align="center">

# Taifeng · 泰逢

**A Python microkernel for LLM agents — skills are markdown, the LLM is the scheduler**

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-622%20passed-brightgreen)](#status)
[![Status](https://img.shields.io/badge/status-pre--alpha-orange)](#status)
[![Style](https://img.shields.io/badge/lint-ruff%20%2B%20mypy-purple)](https://github.com/astral-sh/ruff)

[中文](README.md) · [English](README_EN.md)

</div>

---

> *Classic of Mountains and Seas — Central Mountains*:
> "The auspicious god **Taifeng** presides here. His form is human with a tiger's tail; he dwells on the sunny side of Mount Bei, comes and goes with radiance — **Taifeng moves the qi of heaven and earth**."

The mythological metaphor of Taifeng *moving the unseen qi* maps precisely to what an LLM agent core scheduler manipulates: **the invisible flows of tokens, events, cache, and cancellation**.

---

## What it is

Taifeng is a **business-decoupled** Python LLM agent **microkernel / OS scheduler**, modeled after the CLI agent paradigm of [codex](https://github.com/openai/codex) (Rust), Claude Code (TS), and [claw-code](https://github.com/ultraworkers/claw-code) (Rust) — providing an embeddable agent engine for Python server-side projects.

**It is NOT**:
- ❌ A competitor to LangGraph / AutoGen / Letta (different paradigms: graph / actor / memory)
- ❌ A weaving tool or business framework
- ❌ Bound to any business concepts (no tenant / no domain terms / no LLM provider lock-in)

**It IS**:
- ✅ **Skill is markdown** (not a function tool) — the LLM autonomously reads SKILL.md and expands/dispatches on demand
- ✅ **LLM is the scheduler** (not the scheduled) — the Engine handles concurrency / cancellation / cache / persistence; the LLM decides
- ✅ **Cache-aware compaction** — mid-turn only touches the tail, preserving the cached prefix; head changes are confined to pre-turn
- ✅ **Submission / EventMsg dual-bus actor** — parent-child cascading cancel, submission_id consistent across nested skills
- ✅ **Multi-provider abstraction** — OpenAI / Anthropic / Gemini / any OpenAI-compatible endpoint (via LiteLLM or native httpx)

## Feature Overview

- 🧩 **Skill = markdown** — self-describing SKILL.md; the LLM expands via `read_skill` / dispatches via `call_skill` on demand; atomic + composite recursion with depth / cycle detection
- 🔀 **Declarative orchestration** — parallel / serial / when branches, deterministically driving multi-skill collaboration
- 🗜️ **Cache-aware compaction** — mid-turn touches the tail only to preserve the prompt-cache anchor; handoff (LLM relay) + sliding window strategies
- 🔌 **Multi-provider** — native OpenAI / Anthropic / Gemini / DeepSeek + LiteLLM fallback, a unified `ResponseEvent` stream, accurate prompt-cache hit reporting
- 🛰️ **Bidirectional MCP** — acts as a client (auto-registers tools from external MCP servers) and reverse-exposes skills as an MCP server
- 🔐 **HITL permissions + hooks** — Claude Code-style `Bash(...)` rules, per-builtin approval, 8 hook kinds (PreToolUse / PreCompact / ...)
- 💾 **Persistence & resume** — append-only JSONL primary store + SQLite secondary index, crash-resume by `thread_id`
- ⚡ **Dual-bus actor + cancellable** — Submission / EventMsg message bus, parent-child cascading `CancellationToken`
- 📊 **Observable** — Console / JSONL / OTel (OTLP) sinks, all critical paths instrumented

## Quick Start

```bash
# 1. Install (uv required, not pip)
uv venv && uv pip install -e ".[dev,litellm]"

# 2. Run full test suite (PYTHONPATH=src is required for src-layout)
PYTHONPATH=src uv run pytest tests/

# 3. End-to-end examples (MockClient, no API key needed)
PYTHONPATH=src uv run python examples/basic/minimal_chat.py
PYTHONPATH=src uv run python examples/basic/composite_skill.py

# 4. Real LLM (requires OPENAI_API_KEY etc.)
PYTHONPATH=src uv run python examples/real_llm/e2e.py
```

Minimal skeleton (19 lines of business code + 1 SKILL.md):

```python
import taifeng

# 1) Write a self-describing markdown at ./skills/hello/SKILL.md
# 2) Assemble the Engine on the business side:
pool = await taifeng.EnginePool.create(
    skills_dir="./skills",
    storage_dir="./threads",   # legacy param name threads_dir still works
    model_client=taifeng.LiteLLMClient(model="gpt-4o-mini"),
    compressors=[taifeng.HandoffCompactionStrategy()],
)
engine = await pool.get_or_create(session_id="s1", entry_skill_id="hello")

sub_id = await engine.submit(taifeng.UserMessage(text="Hello"))
async for ev in engine.subscribe(sub_id):
    if ev.msg.kind == "assistant_text":
        print(ev.msg.data["delta"], end="", flush=True)
    elif ev.msg.kind in ("turn_completed", "turn_failed"):
        break

await pool.close()
```

> **Terminology: session / thread / conversation are three distinct layers, not synonyms**
> - **`session_id`** —— a caller-defined logical session key; `EnginePool` uses it to **cache the live Engine instance** (in-process routing, **not persisted**).
> - **`thread_id`** —— the **unit of persistence / resume**: each `create_thread` returns a thread_id, the transcript JSONL is keyed by it, and `resume_thread_id` resumes by it. At runtime, the real identifier of "one conversation" is the thread_id.
> - **`conversation/`** —— only the **name of the persistence subsystem** (module), **not a runtime identifier** (there is no `conversation_id` in the code). One "conversation" physically equals one thread.
>
> In short: day-to-day code identifies by **`thread_id`**; "conversation" refers only to that persistence module. `threads_dir` is the legacy param name, equivalent to the (preferred) `storage_dir`.

More examples → [examples/](examples/) (19 of them, covering MCP / HITL / subagent / oscillation regression / real LLM).

## Core Capabilities

| Capability | Module | Spec |
|---|---|---|
| **Unified Skill Model** — atomic + composite + static/runtime cycle detection | `skill/` | [skill-dispatch](docs/architecture/capabilities/skill-dispatch.md) |
| **Tool System** — RwLock parallel / exclusive scheduling (`parallel_safe` field) | `tool/` | — |
| **Builtin Tools (10)** — `read_skill` / `call_skill` / `file_read` / `file_write` / `shell_exec` / `apply_patch` / `run_in_background` / `wait_for_task` / `run_script` / **`http_request`** | `tool/builtins/` | [tool-builtins-extended](docs/architecture/capabilities/tool-builtins-extended.md) |
| **Hook Lifecycle** — PreToolUse / PostToolUse / PreCompact / PreTurn / PreSkillDispatch / PostSkillDispatch | `hooks/` | [hooks](docs/architecture/capabilities/hooks.md) |
| **PermissionPolicy + HITL** — Claude Code-style `Bash(...)` / `Network(...)` / `Skill(read_*)` rule syntax | `permission/` | [permission-gate](docs/architecture/capabilities/permission-gate.md) |
| **MCP stdio client + server mode** — auto-register external MCP tools / reverse-expose skills as a server | `mcp/` | [mcp-server](docs/architecture/capabilities/mcp-server.md) |
| **Cache-aware Compaction** — Handoff (codex paradigm) + SlidingWindow + cache_stats | `context/strategies/` | — |
| **LLM Structured Output** — `ResponseFormatSpec` + `structured_output` event + 3-provider unified translation | `llm/` | [llm-structured-output](docs/architecture/capabilities/llm-structured-output.md) |
| **Subagent Isolation** — three PermissionPolicy wrapping modes: inherit / auto_deny / auto_allow | `skill/dispatch.py` | — |
| **JSONL Transcript + Resume** — append-only primary store + SQLite secondary index + thread_id resume | `conversation/` | [jsonl-transcript](docs/architecture/capabilities/jsonl-transcript.md) |
| **Instructions Injection** — CLAUDE.md / system_prompt / project_instructions three-layer resolver | `loop/prompt.py` | [instructions-injection](docs/architecture/capabilities/instructions-injection.md) |
| **Telemetry** — ConsoleSink / JsonlSink / **OtelTelemetrySink** (OTLP exporter, opt-in) | `telemetry/` | [telemetry-otel](docs/architecture/capabilities/telemetry-otel.md) |
| **Script Execution (M4)** — dispatch Bash/Python via SKILL.md `scripts:` field, with heuristic deny list | `skill/scripts/` | [script-execution](docs/architecture/capabilities/script-execution.md) |

## Five Red Lines (R1–R5)

Hard constraints all changes must pass (full details in [CLAUDE.md](CLAUDE.md)):

| # | Red Line | Implementation |
| --- | --- | --- |
| **R1 Zero business intrusion** | `src/` forbids business concepts: `tenant_id` / `audience` / domain terms (any language) | Business side injects policy via `AgentPolicy` hooks |
| **R2 Cache friendly** | Compaction must return `CompressionResult { cache_invalidated, anchor_preserved_until }` | mid-turn touches tail only; head changes confined to pre-turn |
| **R3 Observable** | Critical paths must emit `EventMsg` (`turn_started` / `tool_dispatched` / `compaction_attempted` / `cache_break_detected` / `provider_retry`) | Via `TelemetrySink` protocol, backend-agnostic |
| **R4 Cancellable** | Long-running ops must accept `CancellationToken`; subagents derive via `cancel.child()` | Never block the main actor |
| **R5 Resumable** | Default store is append-only JSONL; business side may implement `MessageStore` protocol to back it with a DB | `MessageStore` lives in `conversation/store.py` |

## Architecture at a Glance

```
src/taifeng/
├── skill/        # §1.1 SkillDefinition (atomic/composite) / loader / registry / dispatch / cycle detection
├── tool/         # §1.2 ToolSpec (parallel_safe) / Runtime (RwLock scheduling) / 10 builtins
├── conversation/ # §1.3 ResponseItem / MessageStore protocol / JsonlMessageStore + SQLite sidecar
├── context/      # §1.4 ContextBudget / CompressionStrategy / Handoff + Sliding / cache_stats
├── llm/          # §1.5 ModelClient protocol / ResponseEvent / retry / providers (litellm/openai/mock)
├── loop/         # §1.2 Submission/Op + EventMsg + Engine (main actor) + TurnRunner + Pool + Cancellation
├── hooks/        # PreToolUse / PostToolUse / PreCompact / PreTurn (claw-code paradigm)
├── permission/   # HITL approval: PermissionPolicy + Rule (args_match) + Prompter (CLI / Callback)
├── mcp/          # MCP stdio client + server mode
└── telemetry/    # ConsoleSink + JsonlSink + OtelTelemetrySink (other backends pluggable)
```

Per [ADR 0006 "Unified Skill Model"](docs/decisions/0006-unified-skill-model.md) — no separate `agent/` package; skill-to-skill dispatch lives in `skill/dispatch.py`; composite skills replace the agent concept.

A single turn's data flow:

```
Submission(UserMessage) → AgentEngine enqueues → TurnRunner.run_turn
  ├─ pre-sampling compaction check (head changes allowed)
  ├─ build_prompt (entry_skill body + child skills list [id+description only, no body])
  ├─ ModelClientSession.stream → ResponseEvent stream
  │    ├─ TextDelta → EventMsg.AssistantText
  │    ├─ ToolCallDone(read_skill)  → fetch child skill body inline
  │    ├─ ToolCallDone(call_skill)  → DispatchPolicy.check (depth/cycle/whitelist) → spawn child TurnRunner
  │    ├─ ToolCallDone(other)       → ToolCallRuntime.dispatch (parallel_safe ? read lock : write lock)
  │    └─ Completed → break
  ├─ mid-turn compaction check (tail only, preserving cache anchor)
  └─ MessageStore.append → JSONL flush
→ AgentEngine emit EventMsg.TurnComplete
```

Details in [docs/architecture/overview.md](docs/architecture/overview.md).

## Status

🟢 **M1–M4 + all hermes capability gaps closed** (2026-05-28).

- **622 tests** passing (pytest)
- **14k LOC** src, 15 capability contracts ([`docs/architecture/capabilities/`](docs/architecture/capabilities/README.md))
- Capability gaps vs codex / Claude Code / claw-code / hermes are aligned

Recently closed (most recent first):

| Capability | Change |
|---|---|
| LLM structured output (`structured_output`) | `2026-05-27-llm-structured-output` |
| Composite skill three-level E2E (depth/cycle/stack_path assertions) | [tests/skill/test_composite_e2e.py](tests/skill/test_composite_e2e.py) |
| `http_request` builtin (first user of PermissionScope=network) | `2026-05-27-http-request-builtin` |
| `call_skill` LLM self-stated reason propagated to HITL / EventMsg | `2026-05-27-call-skill-reason-field` |
| PermissionRule args_match + Claude Code-style syntax | `2026-05-27-permission-rule-args-match` |
| OtelTelemetrySink (OTLP exporter) | `2026-05-27-telemetry-otel-sink` |

Open (by priority):

- **P2** `web_search` protocol (unbound — business side injects backend) — wait for demand
- **❓** Memory backends / Multi-agent handoff explicit API — needs R1 boundary decision first, see [hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md)

## Comparison

| Comparison | Shape | Language | Relationship to Taifeng |
|---|---|---|---|
| codex / Claude Code | CLI harness | Rust / TS | **Paradigm reference** — copy design, not code |
| claw-code / openclaw | CLI harness | Rust / TS | **Paradigm reference** |
| LangGraph / AutoGen / Letta | Server-side framework | Python | **Not a replacement** — they are graph / actor / memory paradigms |
| LiteLLM | Provider adapter | Python | **Dependency** — Taifeng treats it as an optional backend |

Gap analysis against the four reference paradigms: [docs/architecture/hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md) and [kernel-gap-analysis.md](docs/architecture/kernel-gap-analysis.md).

## Documentation Map

| Entry | Purpose |
|---|---|
| [CLAUDE.md](CLAUDE.md) | AI collaboration conventions + authoritative R1–R5 definitions |
| [AGENTS.md](AGENTS.md) | Engineering collaboration contract (precedes CLAUDE.md, takes priority on conflicts) |
| [docs/architecture/overview.md](docs/architecture/overview.md) | Architecture overview (modules / data flow / red lines / milestones) |
| [docs/configurable-knobs.md](docs/configurable-knobs.md) | Full list of business-configurable knobs (includes §7 structured_output) |
| [docs/architecture/hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md) | hermes capability gap roadmap |
| [docs/decisions/](docs/decisions/) | 10 ADR decision records |
| [docs/architecture/capabilities/](docs/architecture/capabilities/README.md) | 15 capability contracts (authoritative data structures / protocols / events / constraints) |

## Development Workflow

Contract-first:

```bash
# 1. Define the capability contract first, in
#    docs/architecture/capabilities/<capability>.md
#    (data structures / protocols / events / constraints)

# 2. Implement + run tests
PYTHONPATH=src uv run pytest tests/<relevant>

# 3. Sync the living docs: update the matching docs/architecture/<module>.md

# 4. commit & push
git commit -am "feat: ..."
```

Each task ≤ 3h with Acceptance criteria; tasks touching compaction / cache / dispatch must explicitly state R1–R5 impact.

## Choosing a Provider

```python
# Recommended: unified multi-provider adapter (OpenAI / Anthropic / Gemini / local models)
from taifeng.llm.providers import LiteLLMClient
client = LiteLLMClient(model="gpt-4o-mini")            # OpenAI
client = LiteLLMClient(model="anthropic/claude-3-5-sonnet")
client = LiteLLMClient(model="gemini/gemini-2.0-flash")

# Without LiteLLM dependency: native OpenAI-compat
from taifeng.llm.providers import OpenAICompatClient
client = OpenAICompatClient(
    base_url="https://api.openai.com/v1",  # also supports vLLM / Ollama / DeepSeek
    api_key="sk-...",
    model="gpt-4o-mini",
)

# Testing / offline: Mock
from taifeng.llm.providers import MockClient, MockTurn
client = MockClient(turns=[MockTurn(text="hi", ...)])
```

## License

**Proprietary** (current `pyproject.toml` setting) — no open-source license chosen yet.

If you intend to publish this publicly, first:
1. Update the `license` field in `pyproject.toml`
2. Add a `LICENSE` file (Apache 2.0 / MIT recommended)
3. Audit `examples/` for any real API keys or business data

## Acknowledgments

Design references (**copy paradigm, not code**):

- [openai/codex](https://github.com/openai/codex) — Rust CLI agent; origin of `compact.rs` / `prompt_cache.rs` / `ModelClient` paradigms
- [Anthropic Claude Code](https://claude.com/claude-code) — originator of SKILL.md paradigm + Hook lifecycle
- claw-code — Rust open-source port of Claude Code; tool-pairing boundary protection + permission
- openclaw — TS open-source port of Claude Code; actor + session model
- [LiteLLM](https://github.com/BerriAI/litellm) — multi-provider unified adapter

---

<div align="center">

*"Taifeng moves the qi of heaven and earth"*

[ADR 0001 — Why "Taifeng"](docs/decisions/0001-naming-taifeng.md)

</div>
