# Taifeng Documentation Index

> Entry point for Taifeng research notes, architecture documents, capability contracts, and ADRs.

## For Integrators

If you are integrating Taifeng into a host system rather than changing the kernel, start here:

1. [Capability matrix](capability-matrix.md): what exists, current status, entry APIs, examples, and contracts.
2. [Usage guide](usage.md): installation, usage levels, and code skeletons for each major capability.
3. [Configurable knobs](configurable-knobs.md): construction-time arguments, runtime ops, and kernel control fields.
4. [Real LLM ledger](real-llm-ledger.md): generated regression ledger for real-provider scenarios. Do not edit it manually.

## Reading Order for Kernel Contributors

### First Pass: What Taifeng Is and Why

1. [Architecture overview](architecture/overview.md): the five-dimensional abstraction, six core packages, and infrastructure packages.
2. [ADR 0001: Naming Taifeng](decisions/0001-naming-taifeng.md).
3. [ADR 0002: Choosing Python](decisions/0002-python-language.md).

### Second Pass: How Taifeng Differs from Mainstream Frameworks

4. [Hermes capability gap roadmap](architecture/hermes-gap-roadmap.md): comparison across codex, claw-code, openclaw, and hermes.
5. [ADR 0003: Skill is context, not a tool](decisions/0003-skill-as-context.md).
6. [ADR 0004: Cache-aware compaction](decisions/0004-cache-aware-compression.md).
7. [ADR 0005: Submission / Event dual bus](decisions/0005-submission-event-bus.md).
8. [ADR 0006: Unified skill model, no agent package](decisions/0006-unified-skill-model.md).

### Third Pass: Implementation Details

9. [Skill system](architecture/skill-system.md): §1.1, aligned with ADR 0003 / 0006 / 0009.
10. [Agent loop](architecture/agent-loop.md): §1.2 plus instruction injection, aligned with ADR 0005 / 0007 / 0010.
11. [Conversation persistence](architecture/conversation.md): §1.3, aligned with ADR 0008.
12. [Context compression](architecture/context-compression.md): §1.4, aligned with ADR 0004.
13. [LLM client](architecture/llm-client.md): §1.5.

Later ADRs:

14. [ADR 0007: Instructions as host-side injection](decisions/0007-instructions-as-injection.md).
15. [ADR 0008: Store protocol decoupling and stdlib SQLite index](decisions/0008-store-protocol-decoupling.md).
16. [ADR 0009: SKILL.md scripts runtime](decisions/0009-scripts-runtime.md).
17. [ADR 0010: Permission gate completeness](decisions/0010-permission-gate-completeness.md).
18. [ADRs 0011-0015](decisions/): empty API keys, suspend/resume, composite-tool-only, turn rewind, and detached skill spawn.
19. [ADR 0016: Cold rewind rebuild](decisions/0016-cold-rewind-rebuild.md).
20. [ADR 0017: Kernel positioning criteria](decisions/0017-kernel-positioning-criteria.md).
21. [ADR 0018: Thread-addressable rewind](decisions/0018-thread-addressable-rewind.md).
22. [ADRs 0019-0022](decisions/): post-turn hook, budget-awareness hint, doom-loop detection, and reusable approval grants.
23. [ADR 0023: Skill discovery via search](decisions/0023-skill-discovery-via-search.md): deferred exposure, whitelist-scoped recall, and confidence-as-data.
24. [ADR 0024: Skill recall/verify pipeline](decisions/0024-skill-recall-verify-pipeline.md) (Amends #0023): opt-in auto-discovery toggle, plus a post-recall verification gate judging input-requirement fit.
25. [ADR 0025: SessionJournal as the session source of truth](decisions/0025-session-journal-source-of-truth.md): canonical session history, complete Timeline projection, HITL/approval records, and session-scoped fail-closed durability.
26. [ADR 0026: Codex proxy as an independent provider](decisions/0026-independent-codex-provider.md): explicit `codex-responses-v1` identity, no Chat fallback, and isolated provider state. Its living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md).
27. [ADR 0027: Sensitive LLM request audit](decisions/0027-sensitive-llm-request-audit.md) (Amends #0025): safe projection, redaction manifest, and full canonical request digest without duplicating image or reasoning ciphertext.
28. [ADR 0028: Effect-based permission model](decisions/0028-effect-based-permission-model.md): scope = effect type, target = normalized object; `tool_use` is only the fallback; Style A aliases map to effect scopes. Living contract is [permission-gate](architecture/capabilities/permission-gate.md).
29. [ADR 0029: Root-turn serialization and single writer](decisions/0029-root-turn-serialization-single-writer.md) (Amends #0025): one root turn at a time via a FIFO root gate, runner is the only root-history writer while in flight, every submission gets a terminal event, and audited application is deferred until the token holds the gate.
30. [ADR 0030: Codex SSE noise tolerance](decisions/0030-codex-sse-noise-tolerance.md) (Amends #0026): unregistered or malformed top-level SSE frames — relay-injected keepalives included — are counted and skipped instead of failing the attempt; protocol-internal violations stay fail-closed and the terminal guarantee remains with the completed gate. Living contract is [llm-codex-provider](architecture/capabilities/llm-codex-provider.md) §5.3.
31. [ADR 0031: Terminal replay for late subscribers](decisions/0031-late-subscriber-terminal-replay.md): the engine remembers each submission's last terminal event so a filtered subscription created after the turn already finished gets that real event instead of hanging forever; bounded FIFO, unknown submissions still wait. Living contract is [audit-observability](architecture/capabilities/audit-observability.md).

### Fourth Pass: Gap Tracking

22. [Hermes capability gap roadmap](architecture/hermes-gap-roadmap.md): feature-level progress.
23. [Kernel gap analysis](architecture/kernel-gap-analysis.md): kernel primitive progress.

The two gap documents are complementary: the roadmap answers "which features exist?", while the kernel gap analysis answers "which kernel mechanisms are complete?".

## Documentation Categories

| Directory | Purpose | Lifetime |
| --- | --- | --- |
| `architecture/` | Current architecture and gap analysis, including the capability contract layer | Long-lived; updated as implementation changes |
| `architecture/capabilities/` | Stable field-level contracts for data structures, protocols, events, and constraints | Long-lived; updated with capability changes |
| `decisions/` | ADR decision records | Permanent; append-only |

Use `architecture/` for the current system shape. Use `decisions/` for why a decision was made.

## Maintenance Rules

- Do not rewrite existing ADRs. If a decision changes, write a new ADR and mark the superseded record.
- Keep architecture docs synchronized with implementation changes.
- Capability changes must update the relevant contract and [capability matrix](capability-matrix.md).
- Generated ledgers such as [real-llm-ledger.md](real-llm-ledger.md) should be updated by their generation scripts, not by hand.
- The example tier lists (which demo needs a real LLM key) are owned by `scripts/verify_examples.py`; docs must follow the script, not a hand-maintained list. `.github/workflows/ci.yml` runs the full test suite and the example smoke on every push to main and every PR; real-LLM regression stays manual (see the ledger red line in `AGENTS.md`).
