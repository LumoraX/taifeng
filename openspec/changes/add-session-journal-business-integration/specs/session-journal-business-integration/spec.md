## ADDED Requirements

### Requirement: Audit mode remains explicit and legacy-compatible
The system SHALL enable strict SessionJournal business integration only when an injected audit configuration is present, and SHALL preserve existing EnginePool, AgentEngine, MessageStore, EventMsg, and skill behavior when audit configuration is absent.

#### Scenario: Legacy Session runs without Journal
- **WHEN** an application creates and uses an EnginePool without audit configuration
- **THEN** Session creation, submission enqueue, transcript persistence, events, resume, and tool/skill execution use the existing behavior without creating a SessionJournal

#### Scenario: Audited Session is opt-in
- **WHEN** an application supplies a valid audit configuration for a new Session
- **THEN** the Engine uses the strict Journal-first path and exposes that Session as audit-required

### Requirement: Audited Session bootstrap is Journal-first
The system SHALL preallocate the root thread id and durably create the SessionJournal initialization batch before creating the transcript projection, starting the actor, accepting input, or executing an effect.

#### Scenario: Successful audited bootstrap
- **WHEN** a valid audit-required new Session is created
- **THEN** `session_started`, `thread_created`, and `thread_bound` are durably committed before the transcript projection is created and before EnginePool returns the Engine

#### Scenario: Journal initialization fails
- **WHEN** SessionJournal initialization does not return a definite durable ack
- **THEN** EnginePool does not start or return an execution-capable Engine and no business effect is executed

#### Scenario: Projection bootstrap fails after Journal initialization
- **WHEN** the Journal initialization succeeds but the transcript projection cannot be created
- **THEN** EnginePool invokes the unique Session finish path, attempts durable thread/session terminal records, releases only that Session lease, and reports Engine creation failure

### Requirement: Domain records are versioned, canonical, and deterministic
The system SHALL define frozen extra-forbidden V1 payload DTOs for submission, turn, LLM, Tool, Skill, thread/session terminal, stable error, attachment, and conversation item records, and SHALL convert them to canonical JsonValue before append.

#### Scenario: V1 record identity is retried
- **WHEN** the same logical V1 operation is constructed more than once with identical inputs
- **THEN** it produces the same operation id, attempt id, record id, payload, and idempotent Journal ack

#### Scenario: Same record identity carries changed content
- **WHEN** a V1 record id is reused with different payload or lineage
- **THEN** SessionJournal rejects it as a conflict and does not change the committed tail

#### Scenario: Initialization V0 is read with V1 records
- **WHEN** a Journal contains the Phase 1 V0 initialization records followed by V1 business records
- **THEN** the system preserves the initialization bytes and ids and decodes both versions without rewriting V0

### Requirement: UserMessage acceptance precedes actor enqueue
The system SHALL use one lifecycle/admission lock so that an audit-required UserMessage is canonically validated and atomically committed as `submission_accepted + conversation_item(user_message) + submission_applied` before its accepted token is placed in the actor queue.

#### Scenario: Valid UserMessage is admitted
- **WHEN** an audit-required Session in OPEN state receives a valid UserMessage
- **THEN** the Journal batch receives durable ack before actor enqueue, and the actor later applies only the acknowledged conversation item to hot history and projection

#### Scenario: Invalid UserMessage is rejected
- **WHEN** UserMessage text, attachment, or free structure cannot satisfy the canonical V1 input contract
- **THEN** the system durably writes a safe `submission_rejected`, does not enqueue the submission, does not freeze the Session, and executes no effect

#### Scenario: Enqueue fails after durable acceptance
- **WHEN** the Journal acceptance batch succeeds but the in-memory queue cannot accept the acknowledged token
- **THEN** the Session enters recovery-required and does not silently accept another effect

### Requirement: Attachments are complete and verifiable
The system SHALL accept only inline base64 attachment content with media type, declared size, SHA-256 digest, and injected per-item and total size limits in this slice.

#### Scenario: Inline attachment is valid
- **WHEN** decoded content matches its declared size and SHA-256 and remains within both configured limits
- **THEN** the complete attachment is included in the durable submission payload

#### Scenario: Attachment reference is not durable content
- **WHEN** an attachment contains only a temporary path, missing content, unsupported URI, mismatched digest, mismatched size, or excessive size
- **THEN** the submission is rejected before acceptance and no turn starts

### Requirement: Conversation state is derived only from committed conversation items
The system SHALL atomically commit each domain outcome with its ordered `conversation_item` records and SHALL update hot history and MessageStore materialization only after the covering JournalAck is received.

#### Scenario: Domain outcome and conversation item commit
- **WHEN** an LLM response, Tool outcome, Skill outcome, or user submission changes conversation history
- **THEN** its domain record and corresponding conversation items are committed in one Journal batch before history or projection changes

#### Scenario: Transcript projection fails
- **WHEN** the Journal batch is durable but the MessageStore materialization write fails
- **THEN** Journal and hot history remain authoritative, projected seq remains behind and stale, the Session does not freeze, and replay by Journal seq can rebuild the projection

#### Scenario: Tool output is projected
- **WHEN** an LLM function call is followed by a Tool terminal outcome
- **THEN** the LLM response batch contains the only function call item and the Tool outcome batch contains exactly one linked function call output without duplicating the call

### Requirement: Audited transcript cannot downgrade to legacy resume
The system SHALL mark the default transcript projection with `audit_required`, Journal Session identity, and Journal schema version, and SHALL reject legacy resume of that transcript without a valid Journal recovery path.

#### Scenario: Legacy resume targets an audited transcript
- **WHEN** a caller attempts non-audit resume using a transcript whose metadata marks it audit-required
- **THEN** the system raises a stable audit downgrade error before loading it as execution history or starting an actor

### Requirement: Every LLM network attempt has durable request and checkpoint records
The system SHALL require an attempt-observable ModelClient in audit mode, SHALL durably commit `llm_request_committed` before each actual network dispatch, and SHALL durably commit an attempt-specific `llm_response_checkpoint` before exposing buffered response events or beginning another internal retry.

#### Scenario: Successful LLM attempt
- **WHEN** an observed network attempt produces a complete response
- **THEN** request intent precedes dispatch, the complete normalized response checkpoint precedes every visible delta, and the final logical response commit follows the checkpoint

#### Scenario: Provider retries internally
- **WHEN** an observed attempt fails with a retryable error and the provider will start another network attempt
- **THEN** the failed attempt's error checkpoint receives durable ack before the next attempt's request intent and dispatch

#### Scenario: Attempt result is uncertain
- **WHEN** a dispatched attempt cannot produce a definite checkpoint ack or the observer fails after dispatch
- **THEN** the attempt is UNKNOWN, the Session freezes, no buffered delta is published, and no retry begins

#### Scenario: ModelClient is not attempt-observable
- **WHEN** audit mode is configured with a ModelClient that cannot expose every actual network attempt
- **THEN** capability validation rejects the Session before an LLM effect

### Requirement: Final LLM response commits before downstream effects
The system SHALL atomically commit `llm_response_committed` and ordered reasoning, assistant, and function call conversation items after the final checkpoint and before any Tool effect or turn terminal transition.

#### Scenario: LLM response requests tools
- **WHEN** the final normalized response contains one or more function calls
- **THEN** their conversation items are durable and applied in provider order before Tool intents are committed or dispatched

#### Scenario: LLM response completes without tools
- **WHEN** the final normalized response ends the turn
- **THEN** assistant/reasoning conversation items are durable and applied before `turn_completed`

### Requirement: Every Tool effect has durable intent and terminal convergence
The system SHALL atomically commit ordered `tool_intent_committed` records before dispatch and SHALL converge every committed intent to exactly one `tool_outcome_committed` status of success, error, rejected, cancelled, or unknown under a cancellation-independent bounded finalization scope.

#### Scenario: Tool succeeds
- **WHEN** a permitted Tool returns a definite successful ToolResult
- **THEN** its durable intent precedes dispatch and its outcome plus linked function call output commit before another LLM call

#### Scenario: Tool is not offered
- **WHEN** the model names a Tool that was not in the effective offered set
- **THEN** the system commits intent and rejected outcome without invoking runtime

#### Scenario: Cancellation occurs before Tool dispatch
- **WHEN** a committed intent has not crossed the dispatch gate and its turn is cancelled
- **THEN** the terminal outcome is cancelled and runtime is not invoked

#### Scenario: Cancellation occurs during ambiguous external effect
- **WHEN** cancellation, timeout, or an unexpected exception occurs after dispatch and the system cannot prove whether the external effect occurred
- **THEN** the terminal outcome is unknown, the Session enters recovery-required, and no subsequent effect begins

#### Scenario: Parallel Tools partially complete
- **WHEN** one branch succeeds while another errors, is cancelled, or becomes uncertain
- **THEN** all committed intents receive outcomes in original call-index order and one branch cannot discard another branch's terminal record

### Requirement: Tool audit metadata and strict runtime path are mandatory
The system SHALL require stable ToolSpec effect kind, reconciliation mode, and non-suspending declaration in audit mode and SHALL reject hooks, permission policies, HITL, or suspending Tool capabilities before effect.

#### Scenario: Tool metadata is incomplete
- **WHEN** an offered Tool lacks effect kind or reconciliation metadata
- **THEN** audit capability validation rejects it before intent or runtime dispatch

#### Scenario: Declared non-suspending Tool suspends
- **WHEN** a Tool accepted by audit capability validation nevertheless raises a suspension signal
- **THEN** the system commits an error outcome when possible, freezes the Session as a capability-contract violation, and does not enter HITL

### Requirement: Synchronous call_skill records complete child lineage
The system SHALL use the root Session coordinator and lease for synchronous `call_skill`, SHALL commit the selected Skill snapshot before child dispatch, and SHALL record child thread creation, seed, turn identity, dispatch finish, thread terminal, and Skill outcome.

#### Scenario: call_skill succeeds
- **WHEN** quota accepts a synchronous child Skill
- **THEN** `skill_selected` precedes an atomic started/thread-created/thread-bound/child-seed batch, the child runs from durable seed history, and finished/thread-terminal/skill-outcome commit atomically before the outer Tool outcome

#### Scenario: call_skill quota rejects
- **WHEN** spawn quota rejects the child before thread creation
- **THEN** `skill_selected` is followed by `skill_dispatch_finished(status=rejected, started_record_id=None)` and no child thread or seed record is created

#### Scenario: Child projection fails
- **WHEN** child seed is durable but child transcript materialization fails
- **THEN** projection becomes stale and the child continues from acknowledged hot history

#### Scenario: Nested child completes with error or cancellation
- **WHEN** a child or nested child reaches an error or cancelled terminal state
- **THEN** each level retains stable parent/child lineage and one finished/thread-terminal/skill-outcome batch

### Requirement: Session failures are isolated and fail closed
The system SHALL maintain one coordinator and writer health state per Session, SHALL close the effect gate on the first Journal IO, integrity, or ack-uncertain failure, and SHALL leave other Sessions operational.

#### Scenario: Intent append fails
- **WHEN** an audited effect intent cannot receive definite durable ack
- **THEN** the effect is not dispatched, that Session freezes, and later LLM/Tool/Skill effects in that Session are rejected

#### Scenario: Outcome append fails after effect
- **WHEN** an effect has dispatched but its outcome cannot receive definite durable ack
- **THEN** the effect state is UNKNOWN, that Session freezes, and the system does not automatically retry it

#### Scenario: Another Session is healthy
- **WHEN** one Session freezes because its Journal fails
- **THEN** a different Session with a healthy coordinator can continue submissions and effects

### Requirement: CancelTurn is targeted and Shutdown is Session-scoped
The system SHALL give each active turn a target cancellation subtree, SHALL reserve Session root cancellation for freeze and Shutdown, and SHALL record CancelTurn and Shutdown submission outcomes when Journal health permits.

#### Scenario: One of two turns is cancelled
- **WHEN** CancelTurn targets one active submission while another turn is active
- **THEN** only the target turn and its child effects are cancelled, the other turn remains eligible for later effects, and CancelTurn records its accepted/applied result

#### Scenario: Frozen Session receives emergency cancellation
- **WHEN** Journal is unavailable and CancelTurn or Shutdown is required as a safe degradation action
- **THEN** cancellation/close may proceed without fabricated durable records, introspection reports `audit_complete=false`, and `lease_released` reflects only a definite close result

### Requirement: Session lifecycle finish is unique and deterministic
The system SHALL coordinate admission and lifecycle through `OPEN → FINISHING → CLOSED`, SHALL use one canonical finish value with defensive per-caller result copies, stable terminal record ids, an append-lock-protected terminal seal, and exactly one per-Session close operation. The result and snapshot SHALL report durable terminal acknowledgement as `audit_complete` independently from definite resource release as `lease_released`.

#### Scenario: release and Shutdown race
- **WHEN** release, close, or Shutdown concurrently attempts to move an OPEN Session to FINISHING
- **THEN** one caller wins, closes intake, snapshots all durable-accepted queued/in-flight submissions, and all other lifecycle callers await the same finish future

#### Scenario: New submission arrives while finishing
- **WHEN** lifecycle is FINISHING or CLOSED
- **THEN** the submission is neither durably accepted nor enqueued and receives a stable SessionFinishingError

#### Scenario: Accepted submission remains queued at finish
- **WHEN** a UserMessage received durable acceptance before intake closed but remains in the actor queue
- **THEN** finish converges it before committing `session_ended`

#### Scenario: Completed admission history is pruned
- **WHEN** a long-lived OPEN Session repeatedly admits and completes work
- **THEN** the lifecycle coordinator prunes settled completed reservations before the next admission and finish snapshot, retaining only pending or accepted-incomplete work

#### Scenario: Normal finish completes
- **WHEN** all accepted work and thread terminals have converged
- **THEN** deterministic thread terminal records and one session ended record commit under the terminal seal before `close_session(lease)` is called exactly once, and the result reports `audit_complete=true, lease_released=true`

#### Scenario: Append races with terminal sealing
- **WHEN** an ordinary append races with finish after accepted work convergence
- **THEN** finish reads the latest committed thread-terminal set while holding the append lock, seals before terminal dispatch, deduplicates thread terminals, and rejects every later append before core dispatch so `session_ended` remains the final durable record

#### Scenario: Terminal ack succeeds but close fails
- **WHEN** the terminal batch receives a definite durable ack and `close_session` fails
- **THEN** result and snapshot report `audit_complete=true, lease_released=false`, preserve definite terminal record ids, and expose recovery-required health with a stable release failure

#### Scenario: Terminal append fails but emergency close succeeds
- **WHEN** the terminal batch does not receive a definite durable ack and emergency close definitely releases the lease
- **THEN** result and snapshot report `audit_complete=false, lease_released=true` and do not fabricate terminal record ids

#### Scenario: Finish callers mutate returned values
- **WHEN** one caller mutates its returned finish result or nested stable failure through low-level attribute access
- **THEN** concurrent and later callers receive equal canonical values in distinct result and failure objects without observing that mutation

### Requirement: Unsupported audit capabilities are rejected before effect
The system SHALL allow only new Session, UserMessage, CancelTurn, Shutdown, default JSONL materialization, no hooks/permission/compression/memory/instruction update, atomic/composite synchronous Skill execution, no spawn/peer, an attempt-observable ModelClient, and metadata-complete non-suspending Tools.

#### Scenario: Static audit configuration is unsupported
- **WHEN** audit configuration includes resume, custom store/directory, IndexHook, hooks, permission/HITL, compressor, memory, instruction layers, orchestration, spawn/peer, an unobserved client, or a suspending Tool
- **THEN** EnginePool rejects configuration before Journal-backed business effect

#### Scenario: Dynamic unsupported operation is submitted
- **WHEN** an audit-required Session receives an operation outside UserMessage, CancelTurn, or Shutdown
- **THEN** the submission is safely rejected before the unsupported operation executes

### Requirement: Stable errors do not leak arbitrary exception representations
The system SHALL serialize failures with a versioned stable error containing code, stable class name, failure class, optional approved safe message or descriptor hash, and retryability, and MUST NOT persist arbitrary `repr()`, traceback objects, memory addresses, or secrets.

#### Scenario: Unknown runtime exception is recorded
- **WHEN** an arbitrary exception contains a secret or address-like text
- **THEN** its stable error record contains only approved stable fields and does not include the secret, address, traceback, or raw repr

### Requirement: Repository evidence is mandatory before completion
The system SHALL keep focused audit tests, full pytest, full mypy, relevant Ruff, Sim selfcheck, strict OpenSpec validation, living architecture, capability matrix, and fresh real-LLM ledger evidence as completion gates for this base-layer change.

#### Scenario: Change is marked complete
- **WHEN** all implementation tasks are checked complete
- **THEN** the final code head has successful focused/full automated evidence, capability documents match runtime behavior, and both real-LLM ledger files were regenerated by an authorized successful capability matrix run

#### Scenario: Real provider authorization or run is unavailable
- **WHEN** the real-LLM capability matrix cannot run successfully at the final code head
- **THEN** the change remains incomplete and MUST NOT be archived or merged
