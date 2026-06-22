<div align="center">

<img src="docs/assets/brand/taifeng-favicon-source.png" alt="Taifeng logo" width="120">

# 泰逢 · Taifeng

**A Python microkernel for LLM agents: skills are markdown, the LLM is the scheduler.**

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-622%20passed-brightgreen)](#status)
[![Status](https://img.shields.io/badge/status-pre--alpha-orange)](#status)
[![Style](https://img.shields.io/badge/lint-ruff%20%2B%20mypy-purple)](https://github.com/astral-sh/ruff)

[Full English README](docs/readme/README.en.md) · [简体中文完整说明](docs/readme/README.zh-CN.md) · [All README languages](docs/readme/)

</div>

---

> 《山海经·中山经》：
> "吉神泰逢司之，其状如人而虎尾，是好居于萯山之阳，出入有光，**泰逢神动天地气也**。"

Taifeng keeps its Chinese name because the source metaphor matters. The mythic Taifeng "moves the qi of heaven and earth"; the engineering Taifeng moves the invisible flows that make an LLM agent runtime work: tokens, events, cache anchors, tool calls, cancellation, and persisted turns.

## Positioning

Taifeng is a **business-decoupled Python LLM agent microkernel / OS scheduler**. It is designed for Python server-side systems that need an embeddable agent runtime with explicit control over skills, tools, context, persistence, permissions, and observability.

It follows the CLI-agent paradigm represented by [codex](https://github.com/openai/codex), Claude Code, [claw-code](https://github.com/ultraworkers/claw-code), and openclaw, but ports the core ideas into a Python infrastructure package. Taifeng learns the pattern; it does not copy implementation code.

Taifeng is **not**:

- a LangGraph / AutoGen / Letta replacement;
- a business framework or weaving layer;
- tied to any tenant model, product domain, or LLM provider;
- a memory-first agent platform.

Taifeng is:

- **skill-native**: skills are documented in `SKILL.md`, not hidden behind function-tool wrappers;
- **scheduler-oriented**: the LLM decides, while the engine owns concurrency, cancellation, cache safety, and persistence;
- **cache-aware**: compaction preserves cached prefixes whenever possible;
- **observable and resumable**: every important runtime path emits events, and the default transcript store is append-only JSONL;
- **provider-flexible**: OpenAI-compatible, Anthropic, Gemini, DeepSeek, and LiteLLM-backed models share one event stream shape.

## Features

- **Markdown skills**: `SKILL.md` files are loaded as first-class runtime capabilities. The LLM can lazily `read_skill` and recursively `call_skill`.
- **Composite dispatch**: atomic and composite skills support depth guards, cycle detection, permission checks, and hook gates.
- **Declarative orchestration**: `parallel`, `serial`, and `when` plans run deterministically without extra LLM sampling.
- **Detached spawn and join**: long-running child skills can run independently, suspend for HITL, resume, and join through barriers.
- **Built-in tools**: skill IO, file IO, shell execution, patching, background tasks, script execution, HTTP requests, user input, peer messaging, and todo state.
- **HITL permissions**: typed permission requests support Claude Code-style rules such as `Bash(...)`, `Network(...)`, and `Skill(...)`.
- **Hooks**: pre/post tool, skill, script, turn, and compaction hooks give integrators policy injection points without business concepts in the kernel.
- **Context compression**: handoff, sliding-window, reactive overflow recovery, and surgical trim strategies keep turns inside budget while reporting cache impact.
- **Persistence and resume**: JSONL transcripts are the source of truth, with SQLite side indexes and thread-level resume.
- **LLM conformance simulator**: tests use `SimClient` and golden shape fixtures instead of calling real APIs in CI.
- **MCP integration**: Taifeng can consume external MCP tools and expose skills as an MCP server.
- **Telemetry**: Console, JSONL, and OpenTelemetry sinks cover the critical runtime path.

## Quick Start

```bash
# Install with uv
uv venv
uv pip install -e ".[dev,litellm]"

# Run the test suite
PYTHONPATH=src uv run pytest tests/

# Run API-key-free examples backed by the simulator
PYTHONPATH=src uv run python examples/basic/minimal_chat.py
PYTHONPATH=src uv run python examples/basic/composite_skill.py

# Run a real-LLM example when provider credentials are configured
PYTHONPATH=src uv run python examples/real_llm/e2e.py
```

## Minimal Example

Create a skill at `./skills/hello/SKILL.md`, then wire the engine from the host application:

```python
import taifeng

pool = await taifeng.EnginePool.create(
    skills_dir="./skills",
    storage_dir="./threads",
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

Terminology is intentionally strict:

- `session_id` is an in-process routing key used by `EnginePool`.
- `thread_id` is the persistence and resume unit.
- `conversation/` is the persistence module name, not a runtime `conversation_id`.

## Implemented Capabilities

| Area | Implemented capabilities |
| --- | --- |
| Skill system | Markdown skills, lazy skill reading, recursive dispatch, declarative orchestration, runtime eligibility, script execution |
| Loop and tools | actor-style submission/event bus, cancellation, mid-turn steering, turn rewind, HITL suspend/resume, detached spawn/join, peer messaging |
| Context | cache-aware compaction, handoff summaries, sliding windows, reactive overflow recovery, surgical trim, pinned-state reinjection |
| LLM | OpenAI-compatible, Anthropic, Gemini, DeepSeek, LiteLLM fallback, structured output, retry/error classification, prompt-cache accounting |
| Persistence | JSONL transcript store, SQLite thread directory, rollback, pluggable directory/index hooks |
| Observability | `EventMsg` bus, console sink, JSONL sink, OpenTelemetry sink |
| MCP | stdio client integration and server mode |

For the complete integrator-facing matrix, see [docs/capability-matrix.md](docs/capability-matrix.md).

## Architecture at a Glance

```text
src/taifeng/
├── skill/         # SkillDefinition, loader, registry, dispatch, orchestration
├── tool/          # ToolSpec, runtime scheduling, built-in tools
├── conversation/  # ResponseItem, JSONL store, SQLite side index, rebuild
├── context/       # budgets, compression strategies, cache stats, memory
├── llm/           # ModelClient protocol, events, retry, providers, simulator
├── loop/          # AgentEngine, TurnRunner, EnginePool, cancellation, events
├── hooks/         # lifecycle hooks
├── permission/    # HITL approval and rules
├── mcp/           # MCP client/server integration
└── telemetry/     # console, JSONL, OTel sinks
```

The core turn flow is:

```text
Submission(UserMessage)
  -> AgentEngine queue
  -> TurnRunner
  -> pre-sampling compaction
  -> prompt build
  -> ModelClient event stream
  -> tool / skill dispatch
  -> mid-turn cache-aware compaction
  -> JSONL append
  -> EventMsg turn completion
```

## TODO / Roadmap

The current branch has closed the main P0/P1/P2 kernel gaps tracked in the architecture docs. Remaining work is intentionally demand-driven:

- **`web_search` protocol**: define an unbound search capability that business systems can back with their own provider.
- **Memory backends**: decide the R1 boundary for long-term memory before adding richer default implementations.
- **Explicit multi-agent handoff API**: design a stable API only after the ownership boundary is clear.
- **Capability contract translation**: this pass translates indexes; individual contract bodies can be translated later.

See [docs/architecture/hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md) and [docs/architecture/kernel-gap-analysis.md](docs/architecture/kernel-gap-analysis.md) for the detailed gap history.

## Documentation

| Entry | Purpose |
| --- | --- |
| [docs/readme/](docs/readme/) | Full README variants by language |
| [docs/README.md](docs/README.md) | Documentation index and reading order |
| [docs/capability-matrix.md](docs/capability-matrix.md) | Integrator-facing capability matrix |
| [docs/usage.md](docs/usage.md) | Installation and usage guide |
| [docs/configurable-knobs.md](docs/configurable-knobs.md) | Runtime and construction-time configuration |
| [docs/architecture/overview.md](docs/architecture/overview.md) | Architecture overview |
| [docs/architecture/capabilities/](docs/architecture/capabilities/) | Stable capability contracts |
| [docs/decisions/](docs/decisions/) | ADR decision records |
| [examples/](examples/) | End-to-end examples, mostly simulator-backed |
| [docs/assets/brand/](docs/assets/brand/) | Logo, avatar, and favicon source assets |
| [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) | Engineering collaboration rules |

## Development Notes

- Python 3.12+.
- Tests use `pytest` with simulator-backed LLM clients.
- New core behavior should update the matching architecture live doc.
- Capability changes should update both the contract and [docs/capability-matrix.md](docs/capability-matrix.md).
- Real-LLM regressions are tracked in [docs/real-llm-ledger.md](docs/real-llm-ledger.md).

## Status

- Pre-alpha infrastructure package.
- Current recorded suite: **622 tests passing**.
- Source size: roughly **14k LOC** under `src/`.
- The first production user is a host business system, but Taifeng itself must remain domain-free.

## License

Apache License 2.0. See [LICENSE](LICENSE).
