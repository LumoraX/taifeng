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
