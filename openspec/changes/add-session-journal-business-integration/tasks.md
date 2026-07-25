## 1. Contract and durable-core lifecycle

- [x] 1.1 Add the living `session-journal-business-integration` capability contract and register it in the capability index
- [x] 1.2 Write failing tests for lease validation, close-vs-append ordering, per-Session isolation, and repeated close behavior
- [x] 1.3 Implement `JsonlSessionJournalCore.close_session(lease)` without writing domain facts or closing the caller-owned global core
- [x] 1.4 Run the complete SessionJournal test suite, focused Ruff, and focused mypy; commit the core lifecycle slice

## 2. Versioned domain records

- [x] 2.1 Write failing tests for V1 DTO required/extra fields, enums, V0 initialization compatibility, and canonical vectors
- [x] 2.2 Implement stable submission, turn, LLM, Tool, Skill, thread/session, attachment, and conversation-item payload DTOs
- [x] 2.3 Implement deterministic operation/attempt/record identities and conflict-preserving JournalRecord factory helpers
- [x] 2.4 Implement explicit ResponseItem serializers/deserializers and reject unknown item kinds before effect
- [x] 2.5 Implement secret-safe `StableErrorV1` mapping without arbitrary repr, traceback, address, or secret persistence
- [x] 2.6 Implement inline attachment base64/size/SHA-256 validation with injected per-item and total limits
- [x] 2.7 Run record contract tests, focused Ruff/mypy, and commit the record slice

## 3. Durable conversation projection

- [x] 3.1 Write failing tests for durable-ack-only projection, explicit thread id bootstrap, seq ordering, idempotent replay, and stale watermark
- [x] 3.2 Add default JSONL transcript projection bootstrap using a caller-provided thread id and audited metadata
- [x] 3.3 Implement `JournalConversationProjector` accepting only acknowledged `conversation_item` envelopes
- [x] 3.4 Make projection failure return stale state without freezing Journal execution and prove replay reconstructs the same ordered history
- [x] 3.5 Run projector plus legacy transcript tests and commit the projection slice

## 4. SessionAuditCoordinator

- [x] 4.1 Write failing tests for expected-seq serialization, first-failure stability, effect gate freeze, root/target cancellation, and two-Session isolation
- [x] 4.2 Implement coordinator append/batch methods that advance seq only from durable ack and atomically freeze on Journal uncertainty
- [x] 4.3 Implement projection-stale tracking independent of Journal health
- [x] 4.4 Write failing lifecycle tests for OPEN/FINISHING/CLOSED, accepted work snapshots, concurrent finish callers, deterministic terminal ids, and single close
- [x] 4.5 Implement the shared admission/lifecycle lock, one finish future, terminal batch, emergency close, and `audit_complete` introspection
- [x] 4.6 Run coordinator tests, focused Ruff/mypy, and commit the coordinator slice

## 5. Audit configuration and Session bootstrap

- [x] 5.1 Write failing configuration tests for resume, custom store/directory, IndexHook, hooks, permission/HITL, compressor, memory, instructions, orchestration, spawn/peer, unobserved client, and incomplete Tool metadata
- [x] 5.2 Implement injected `AuditConfig` and static capability validation without reading environment variables
- [x] 5.3 Write failing EnginePool tests proving Journal initialization and projection bootstrap precede Engine construction/start
- [x] 5.4 Implement preallocated root thread identity, `create_session`, coordinator construction, audited projection bootstrap, and failure cleanup
- [x] 5.5 Add audited transcript marker downgrade protection to legacy resume
- [x] 5.6 Run capability/bootstrap plus legacy EnginePool/resume tests and commit the bootstrap slice

## 6. Journal-first submissions and lifecycle operations

- [x] 6.1 Write failing tests proving UserMessage durable acceptance precedes enqueue and actor history/projection application follows ack
- [x] 6.2 Implement accepted submission tokens and admission-lock canonical validation in `AgentEngine.submit()` while preserving legacy enqueue behavior
- [x] 6.3 Write failing tests for invalid input rejection, enqueue-after-ack failure, FINISHING rejection, and accepted-but-queued finish convergence
- [x] 6.4 Implement invalid submission records, recovery-required enqueue failure handling, intake closure, and accepted work convergence
- [x] 6.5 Write failing tests proving CancelTurn cancels only its target turn/child subtree while another turn remains effect-capable
- [x] 6.6 Implement CancelTurn accepted/applied records and target cancellation semantics including not-found/already-terminal outcomes
- [x] 6.7 Write failing tests for release-vs-Shutdown, two Shutdown ids, one finish future, stable terminal ids, and one lease close
- [x] 6.8 Implement unique Shutdown admission, Session root cancellation, EnginePool-owned finish, and emergency safe degradation
- [x] 6.9 Run admission/cancellation/lifecycle plus legacy engine tests and commit the submission slice

## 7. Audited LLM attempts and visible responses

- [x] 7.1 Write failing tests proving `llm_request_committed` precedes actual dispatch and attempt identity increments deterministically
- [x] 7.2 Implement the ModelAttemptObserver protocol and attempt-observable client adapter for current one-network-attempt streams
- [x] 7.3 Write failing tests proving complete/error checkpoints receive durable ack before visible deltas or another internal retry
- [x] 7.4 Implement ordered event buffering, cancellation-independent checkpoint finalization, ack-before-release, and UNKNOWN freeze on observer failure
- [x] 7.5 Write failing TurnRunner tests for atomic final response plus ordered reasoning/assistant/function-call conversation items
- [x] 7.6 Implement `llm_response_committed` and acknowledged hot-history/projection application before Tool effect or terminal turn
- [x] 7.7 Run audited LLM, SimClient, and legacy LLM/engine tests; commit the LLM slice

## 8. Audited Tool convergence

- [x] 8.1 Write failing tests for ToolSpec audit metadata, missing metadata rejection, hooks/permission/suspension rejection, invalid JSON, and not-offered Tool handling
- [x] 8.2 Add backward-compatible effect kind, reconciliation, and suspension metadata; classify all built-ins allowed in strict mode
- [x] 8.3 Write failing tests proving ordered Tool intents are durable before dispatch for serial and parallel batches
- [x] 8.4 Implement strict preflight and atomic intent batch before any runtime task starts
- [x] 8.5 Write failing cancellation-window tests for intent-after/pre-dispatch, in-effect uncertainty, post-effect success, sibling exception, and partial completion
- [x] 8.6 Replace fail-fast gather with cancellation-independent ordered terminal convergence that gives every committed intent one outcome
- [x] 8.7 Atomically commit Tool outcomes and exactly one linked function-call-output item per call without duplicating function calls
- [x] 8.8 Freeze before another effect when any Tool outcome is UNKNOWN
- [x] 8.9 Run audited/legacy Tool tests and commit the Tool slice

## 9. Synchronous call_skill lineage

- [x] 9.1 Write failing tests for full Skill snapshot selection, quota rejection without started/thread records, and parent/child operation identities
- [x] 9.2 Commit `skill_selected` after outer Tool intent and implement quota rejection terminal records
- [x] 9.3 Write failing tests for atomic started/thread-created/thread-bound/child-seed records and projection-stale child execution
- [x] 9.4 Implement preallocated child identity, shared root coordinator/lease, acknowledged hot child history, and child turn identity
- [x] 9.5 Write failing success/error/cancel tests for atomic finished/thread-terminal/skill-outcome and outer Tool outcome ordering
- [x] 9.6 Implement child terminal convergence, three-level lineage, and capability-contract freeze on unexpected suspension
- [x] 9.7 Run audited call_skill plus existing composite/dispatch/outcome tests and commit the Skill slice

## 10. Dynamic capability and end-to-end verification

- [x] 10.1 Add one effect-spy test for every unsupported dynamic Op and capability path
- [x] 10.2 Implement submission and per-effect dynamic gates with stable rejection/failure records before effect
- [x] 10.3 Add exact Journal sequence tests for plain assistant, basic Tool, synchronous call_skill, provider error, Tool error, targeted cancel, Journal intent failure, outcome UNKNOWN, and projection stale
- [x] 10.4 Add a two-Session integration test proving one frozen Session does not block another
- [x] 10.5 Run all focused Journal/loop/llm/tool/skill audit tests and the full legacy test suite; commit the integration slice

## 11. Living documentation and capability registration

- [x] 11.1 Update `docs/architecture/conversation.md` with Journal authority, projection, lifecycle, and current recovery exclusions
- [x] 11.2 Update `docs/architecture/agent-loop.md` with admission, effect gates, Tool/Skill convergence, cancellation, and Session isolation
- [x] 11.3 Update `docs/architecture/llm-client.md` with attempt observer and checkpoint-before-delta semantics
- [x] 11.4 Register the LLM strategy capability and real-LLM verification scenarios in `docs/capability-matrix.md`
- [x] 11.5 Ensure the capability contract/index and OpenSpec artifacts match the delivered runtime behavior

## 12. Repository gates and completion evidence

- [x] 12.1 Run `git diff --check`, focused Ruff for all changed files, and full mypy over `src/taifeng`
- [x] 12.2 Run all focused audit suites and full `PYTHONPATH=src .venv/bin/pytest tests/ -q`
- [x] 12.3 Run `examples/real_llm/selfcheck.py` with SimClient and zero provider cost
- [x] 12.4 Run `openspec validate add-session-journal-business-integration --strict`
- [ ] 12.5 Obtain informed external-provider authorization, run the full real-LLM capability matrix at the final code head, and regenerate both ledger files
- [ ] 12.6 Run an independent code/spec audit; fix every Critical/Important finding and rerun affected gates
- [ ] 12.7 Mark only evidenced tasks complete; do not archive or merge without explicit user instruction
