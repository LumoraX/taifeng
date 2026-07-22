# SessionJournal Business Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect new audit-required Sessions to SessionJournal for `UserMessage → LLM → Tool/call_skill → assistant`, with durable intent/outcome ordering, checkpoint-before-delta, per-Session fail-closed behavior, and no regression to legacy Sessions.

**Architecture:** Keep SessionJournal as the only reliable execution and conversation fact source. Add versioned record DTOs, a Journal-backed conversation projector, and one SessionAuditCoordinator per active Session; inject them through EnginePool, AgentEngine, TurnRunner, the model-attempt adapter, tool batching, and synchronous `call_skill`. Audit-required mode is deliberately gated to the approved narrow capability matrix, while the current non-audit path remains unchanged.

**Tech Stack:** Python 3.12+, Pydantic v2 frozen models, anyio cancellation shields and worker-thread IO, asyncio-compatible actor primitives, RFC 8785 canonical JSON, JSONL SessionJournal, pytest/SimClient, Ruff, mypy, OpenSpec.

---

## File structure

- `openspec/changes/add-session-journal-business-integration/`: proposal, design, task ledger, and delta capability spec.
- `docs/architecture/capabilities/session-journal-business-integration.md`: authoritative runtime contract for the delivered slice.
- `docs/architecture/capabilities/README.md`: capability index entry.
- `src/taifeng/conversation/journal/records.py`: versioned domain payload DTOs, stable errors, identities, and record factory.
- `src/taifeng/conversation/journal/projector.py`: durable-ack-only materialized transcript projection and stale watermark.
- `src/taifeng/conversation/journal/jsonl.py`: add per-Session lease-safe close without changing global ownership.
- `src/taifeng/loop/audit.py`: SessionAuditCoordinator, lifecycle/admission state, health gate, and finish convergence.
- `src/taifeng/loop/audit_config.py`: strict audit mode configuration and unsupported-capability validation.
- `src/taifeng/loop/pool.py`: Journal bootstrap, preallocated thread identity, coordinator ownership, release/close integration.
- `src/taifeng/loop/engine.py`: acceptance-before-enqueue submission gateway, target cancellation, and coordinator injection.
- `src/taifeng/loop/turn.py`: durable turn/LLM/Tool/Skill ordering and hot-history application.
- `src/taifeng/loop/tool_batch.py`: cancellation-independent terminal convergence for every committed tool intent.
- `src/taifeng/llm/audit.py`: model-attempt observer/decorator and checkpoint-before-visible-delta buffer.
- `src/taifeng/tool/spec.py`: stable audit metadata for ToolSpec.
- `src/taifeng/conversation/transcript.py`: explicit-id materialized projection bootstrap.
- `tests/conversation/journal/test_records.py`: DTO, identity, serializer, attachment, and stable-error vectors.
- `tests/conversation/journal/test_projector.py`: ack-only projection, replay, and stale-watermark tests.
- `tests/conversation/journal/test_close_session.py`: per-Session close and ownership/race tests.
- `tests/loop/test_audit_coordinator.py`: freeze isolation, lifecycle state, finish, and admission tests.
- `tests/loop/test_audit_capability_gate.py`: static/dynamic unsupported-path rejection tests.
- `tests/loop/test_audit_engine_integration.py`: bootstrap, submission ordering, projection failure, and Session isolation.
- `tests/llm/test_audit_attempts.py`: attempt intent/checkpoint ordering, retry, cancellation, and delta visibility.
- `tests/loop/test_audit_tool_batch.py`: durable tool intent/outcome convergence and no duplicate function calls.
- `tests/skill/test_audit_call_skill.py`: child lineage, quota, projection stale, skill outcome, and terminal records.

### Task 1: Freeze the OpenSpec and capability contract

**Files:**
- Create: `openspec/changes/add-session-journal-business-integration/proposal.md`
- Create: `openspec/changes/add-session-journal-business-integration/design.md`
- Create: `openspec/changes/add-session-journal-business-integration/tasks.md`
- Create: `openspec/changes/add-session-journal-business-integration/specs/session-journal-business-integration/spec.md`
- Create: `docs/architecture/capabilities/session-journal-business-integration.md`
- Modify: `docs/architecture/capabilities/README.md`

- [ ] **Step 1: Create the OpenSpec change and write the proposal**

Use change id `add-session-journal-business-integration`. The proposal must state this exact boundary:

```markdown
## Scope

New audit-required Sessions only. Supported operations are UserMessage, CancelTurn,
and Shutdown. Supported effects are one observed LLM attempt, non-suspending basic
tools, and synchronous call_skill. Existing Sessions and non-audit EnginePool behavior
remain unchanged.
```

- [ ] **Step 2: Write the delta specification scenarios**

The spec must include SHALL scenarios for:

```text
durable acceptance before enqueue
durable LLM request before dispatch
durable response checkpoint before visible delta
durable tool intent before dispatch
one durable terminal outcome or UNKNOWN per committed intent
domain outcome and conversation_item in one atomic batch
one Session frozen without freezing another Session
unsupported capability rejected before effect
audit marker prevents legacy resume downgrade
```

- [ ] **Step 3: Write the capability contract and index entry**

Copy the approved V1 payload tables and exact success/error/cancel/freeze sequences from `docs/superpowers/specs/2026-07-22-journal-business-integration-design.md`. Add this index row:

```markdown
| [session-journal-business-integration](session-journal-business-integration.md) | Experimental strict runtime slice for new Sessions: Journal-first submissions, LLM/Tool/call_skill intent and outcome, durable conversation items, and per-Session fail-closed gating |
```

- [ ] **Step 4: Validate the contract**

Run: `openspec validate add-session-journal-business-integration --strict`

Expected: `Change 'add-session-journal-business-integration' is valid` and exit 0. A telemetry DNS warning is non-blocking only if local validation exits 0.

- [ ] **Step 5: Commit the contract**

```bash
git add openspec/changes/add-session-journal-business-integration docs/architecture/capabilities/session-journal-business-integration.md docs/architecture/capabilities/README.md
git commit -m "docs(conversation): specify journal business integration"
```

### Task 2: Add lease-safe per-Session close to the durable core

**Files:**
- Modify: `src/taifeng/conversation/journal/jsonl.py`
- Modify: `src/taifeng/conversation/journal/__init__.py`
- Create: `tests/conversation/journal/test_close_session.py`

- [ ] **Step 1: Write failing close tests**

```python
@pytest.mark.anyio
async def test_close_session_removes_only_matching_live_writer(tmp_path: Path) -> None:
    journal = JsonlSessionJournalCore(tmp_path)
    first = await journal.create_session(_descriptor("ses_1"))
    second = await journal.create_session(_descriptor("ses_2"))

    await journal.close_session(first.lease)

    with pytest.raises(JournalLeaseError):
        await journal.append(_record("ses_1"), lease=first.lease, expected_seq=3)
    ack = await journal.append(_record("ses_2"), lease=second.lease, expected_seq=3)
    assert ack.last_seq == 4


@pytest.mark.anyio
async def test_close_session_rejects_wrong_lease(tmp_path: Path) -> None:
    journal = JsonlSessionJournalCore(tmp_path)
    created = await journal.create_session(_descriptor("ses_1"))
    wrong = created.lease.model_copy(update={"lease_id": "wrong"})
    with pytest.raises(JournalLeaseError):
        await journal.close_session(wrong)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_close_session.py -v`

Expected: FAIL because `JsonlSessionJournalCore.close_session` does not exist.

- [ ] **Step 3: Implement the minimal close operation**

Add a closed flag to `_LiveWriter` and implement this public method under the registry lock, then the writer lock:

```python
async def close_session(self, lease: SessionLease) -> None:
    """验证 lease 后只释放一个 Session 的 live writer。"""
    async with self._registry_lock:
        writer = self._writers.get(lease.session_id)
        if writer is None:
            raise JournalLeaseError(lease.session_id)
        async with writer.lock:
            self._validate_lease(writer, lease)
            writer.closed = True
            self._writers.pop(lease.session_id, None)
```

All append paths must reject a closed writer. `close_session()` writes no domain record and never calls global `close()`.

- [ ] **Step 4: Run GREEN and focused quality checks**

```bash
PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_close_session.py tests/conversation/journal -q
.venv/bin/ruff check src/taifeng/conversation/journal/jsonl.py tests/conversation/journal/test_close_session.py
PYTHONPATH=src .venv/bin/mypy src/taifeng/conversation/journal
```

Expected: all Journal tests pass; Ruff and mypy exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/conversation/journal/jsonl.py src/taifeng/conversation/journal/__init__.py tests/conversation/journal/test_close_session.py
git commit -m "feat(conversation): close individual journal sessions"
```

### Task 3: Implement versioned domain records and stable identities

**Files:**
- Create: `src/taifeng/conversation/journal/records.py`
- Modify: `src/taifeng/conversation/journal/__init__.py`
- Create: `tests/conversation/journal/test_records.py`

- [ ] **Step 1: Write failing identity and DTO tests**

```python
def test_turn_identity_includes_thread_and_submission() -> None:
    ids = JournalIdentities(session_id="ses", thread_id="thr", submission_id="sub")
    assert ids.turn(2) == "thr:sub:turn:2"


def test_record_id_is_deterministic() -> None:
    assert record_id("op", "turn_started", ordinal=0) == (
        "op:turn_started:none:0"
    )


def test_submission_payload_rejects_wrong_shape() -> None:
    with pytest.raises(ValidationError):
        SubmissionAcceptedV1(op_kind="cancel_turn", text="wrong")


def test_attachment_requires_verified_inline_content() -> None:
    raw = base64.b64encode(b"abc").decode()
    item = AttachmentV1(
        kind="file", media_type="text/plain", size=3,
        sha256=hashlib.sha256(b"abc").hexdigest(), content=raw,
    )
    assert item.decoded() == b"abc"
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_records.py -v`

Expected: collection fails because `records.py` is absent.

- [ ] **Step 3: Implement frozen DTOs and the record factory**

Use one base model and explicit discriminated submission models:

```python
class PayloadModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    payload_version: Literal[1] = 1


class StableErrorV1(PayloadModel):
    code: str
    class_name: str
    failure_class: str
    safe_message: str | None = None
    descriptor_hash: str | None = None
    retryable: bool = False


def record_id(
    operation_id: str,
    record_type: str,
    *,
    attempt_id: str | None = None,
    ordinal: int = 0,
) -> str:
    return f"{operation_id}:{record_type}:{attempt_id or 'none'}:{ordinal}"
```

Define every V1 payload named in the approved design. Keep Phase 1 initialization V0 records untouched. Convert payload models with `model_dump(mode="json")` and pass them through existing canonical validation before constructing `JournalRecord`.

- [ ] **Step 4: Add serializer and secret-safe error tests**

```python
def test_unknown_response_item_kind_is_rejected() -> None:
    item = ResponseItem(kind="future_kind", thread_id="thr")  # type: ignore[arg-type]
    with pytest.raises(UnsupportedConversationItemError):
        conversation_item_record(item, source_record_id="source", identity=_identity())


def test_arbitrary_exception_never_uses_repr() -> None:
    exc = RuntimeError("secret=token-123 object at 0xDEADBEEF")
    stable = stable_error(exc)
    dumped = stable.model_dump_json()
    assert "token-123" not in dumped
    assert "0xDEADBEEF" not in dumped
```

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_records.py -q
.venv/bin/ruff check src/taifeng/conversation/journal/records.py tests/conversation/journal/test_records.py
PYTHONPATH=src .venv/bin/mypy src/taifeng/conversation/journal/records.py
git add src/taifeng/conversation/journal/records.py src/taifeng/conversation/journal/__init__.py tests/conversation/journal/test_records.py
git commit -m "feat(conversation): add journal domain records"
```

### Task 4: Add the durable-ack-only conversation projector

**Files:**
- Create: `src/taifeng/conversation/journal/projector.py`
- Modify: `src/taifeng/conversation/transcript.py`
- Create: `tests/conversation/journal/test_projector.py`

- [ ] **Step 1: Write failing projection tests**

```python
@pytest.mark.anyio
async def test_projector_accepts_only_committed_conversation_records(tmp_path: Path) -> None:
    store = JsonlMessageStore(tmp_path / "view")
    projector = JournalConversationProjector(store)
    with pytest.raises(ProjectionOrderError):
        await projector.apply([_conversation_envelope(seq=4)], ack=_ack(5, 5))


@pytest.mark.anyio
async def test_projection_failure_marks_stale_without_raising() -> None:
    projector = JournalConversationProjector(_FailingStore())
    result = await projector.apply([_conversation_envelope(seq=4)], ack=_ack(4, 4))
    assert result.stale is True
    assert result.projected_seq == 0


@pytest.mark.anyio
async def test_replay_is_idempotent_by_item_id(tmp_path: Path) -> None:
    projector = JournalConversationProjector(JsonlMessageStore(tmp_path / "view"))
    envelope = _conversation_envelope(seq=4)
    await projector.apply([envelope], ack=_ack(4, 4))
    await projector.apply([envelope], ack=_ack(4, 4))
    assert len(await _load_items(projector, "thr")) == 1
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_projector.py -v`

Expected: import failure for `JournalConversationProjector`.

- [ ] **Step 3: Implement explicit-id bootstrap and projection result**

Add an internal explicit-id thread bootstrap to `JsonlMessageStore`:

```python
async def create_projection_thread(
    self,
    *,
    thread_id: str,
    cwd: str | None,
    entry_skill_id: str,
    source: str,
    extra: dict[str, Any],
) -> str:
    return await self._create_thread_with_id(
        thread_id=thread_id, cwd=cwd, entry_skill_id=entry_skill_id,
        source=source, extra=extra,
    )
```

The projector must accept `JournalEnvelope` values whose ids appear in the supplied durable ack, deserialize only explicit `conversation_item` V1 payloads, deduplicate item ids, update per-thread projected seq after a successful write, and return `ProjectionResult(stale=True, ...)` on materialization errors.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONPATH=src .venv/bin/pytest tests/conversation/journal/test_projector.py tests/conversation/test_jsonl_writer.py -q
.venv/bin/ruff check src/taifeng/conversation/journal/projector.py src/taifeng/conversation/transcript.py tests/conversation/journal/test_projector.py
git add src/taifeng/conversation/journal/projector.py src/taifeng/conversation/transcript.py tests/conversation/journal/test_projector.py
git commit -m "feat(conversation): project committed journal items"
```

### Task 5: Implement SessionAuditCoordinator and lifecycle convergence

**Files:**
- Create: `src/taifeng/loop/audit.py`
- Create: `tests/loop/test_audit_coordinator.py`

- [ ] **Step 1: Write failing health and isolation tests**

```python
@pytest.mark.anyio
async def test_failed_append_freezes_only_own_coordinator() -> None:
    first = _coordinator(core=_FailingCore())
    second = _coordinator(core=_RecordingCore())
    with pytest.raises(SessionAuditFrozenError):
        await first.record(_record())
    await second.ensure_effect_allowed()
    assert first.health is AuditHealth.RECOVERY_REQUIRED
    assert second.health is AuditHealth.HEALTHY


@pytest.mark.anyio
async def test_concurrent_finish_calls_share_one_terminal_batch() -> None:
    coordinator, core = _coordinator_with_recording_core()
    first, second = await asyncio.gather(
        coordinator.finish(reason="release"),
        coordinator.finish(reason="release"),
    )
    assert first == second
    assert core.close_session_calls == 1
    assert [r.record_type for r in core.committed].count("session_ended") == 1
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_coordinator.py -v`

Expected: import failure for `taifeng.loop.audit`.

- [ ] **Step 3: Implement coordinator state and append gates**

```python
class AuditHealth(StrEnum):
    HEALTHY = "healthy"
    RECOVERY_REQUIRED = "recovery_required"


class LifecycleState(StrEnum):
    OPEN = "open"
    FINISHING = "finishing"
    CLOSED = "closed"


async def ensure_effect_allowed(self) -> None:
    if self._health is not AuditHealth.HEALTHY:
        raise SessionAuditFrozenError(self.session_id, self._first_failure)
    if self._lifecycle is not LifecycleState.OPEN:
        raise SessionFinishingError(self.session_id)
```

Use a per-Session append lock for expected seq, a shared admission/lifecycle lock for `OPEN → FINISHING → CLOSED`, a Session root CancellationToken, a mapping of target-turn tokens, and one finish task/future. Any Journal IO/integrity/ack-uncertain exception records the first stable failure, closes the effect gate, and cancels the Session root token.

- [ ] **Step 4: Add deterministic finish and projection-stale tests**

Assert terminal record ids use `{session_id}:lifecycle:end`, sorted thread ids, and the approved ordinal formula. Assert `mark_projection_stale()` does not change audit health or cancel an effect.

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_coordinator.py -q
.venv/bin/ruff check src/taifeng/loop/audit.py tests/loop/test_audit_coordinator.py
PYTHONPATH=src .venv/bin/mypy src/taifeng/loop/audit.py
git add src/taifeng/loop/audit.py tests/loop/test_audit_coordinator.py
git commit -m "feat(loop): coordinate journal audit lifecycle"
```

### Task 6: Gate audit configuration and bootstrap new Sessions

**Files:**
- Create: `src/taifeng/loop/audit_config.py`
- Modify: `src/taifeng/loop/pool.py`
- Modify: `src/taifeng/conversation/transcript.py`
- Create: `tests/loop/test_audit_capability_gate.py`
- Create: `tests/loop/test_audit_engine_integration.py`

- [ ] **Step 1: Write failing static gate tests**

```python
@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"resume_thread_id": "old"}, "audit_resume_unsupported"),
        ({"hooks": object()}, "audit_hooks_unsupported"),
        ({"permission_policy": object()}, "audit_permission_unsupported"),
        ({"memory_store": object()}, "audit_memory_unsupported"),
    ],
)
async def test_audit_mode_rejects_unsupported_configuration(
    pool_factory: Callable[..., Awaitable[EnginePool]], override: dict[str, object], code: str
) -> None:
    with pytest.raises(AuditCapabilityError, match=code):
        await pool_factory(audit_required=True, **override)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_capability_gate.py -v`

Expected: FAIL because audit configuration is absent.

- [ ] **Step 3: Implement injected audit configuration**

```python
class AuditConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )
    journal_core: JsonlSessionJournalCore
    writer_id: str
    max_attachment_bytes: int = Field(gt=0)
    max_total_attachment_bytes: int = Field(gt=0)
```

Add `audit: AuditConfig | None = None` to `EnginePool.create()`/`__init__()`. Validate the approved matrix before creating any Journal or transcript. Do not use `os.getenv`; all values are injected.

- [ ] **Step 4: Implement Journal-first bootstrap**

For audit-required mode only:

```python
thread_id = new_thread_id()
created = await journal.create_session(
    SessionDescriptor(
        session_id=session_id,
        creation_operation_id=f"{session_id}:create",
        writer_id=audit.writer_id,
        root_thread=RootThreadDescriptor(
            thread_id=thread_id, entry_skill_id=entry_skill_id,
            source=f"session:{session_id}",
        ),
        config=canonical_engine_config,
    )
)
coordinator = SessionAuditCoordinator(...)
await projector.bootstrap(
    thread_id=thread_id,
    metadata={"audit_required": True, "journal_session_id": session_id,
              "journal_schema_version": 1},
)
```

Only return/start AgentEngine after both Journal initialization and projection bootstrap. On projection bootstrap failure call the unique coordinator finish path.

- [ ] **Step 5: Run GREEN and legacy regression**

```bash
PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_capability_gate.py tests/loop/test_audit_engine_integration.py tests/loop/test_engine_resume.py tests/loop/test_engine_e2e.py -q
```

Expected: audit tests pass and legacy tests remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/loop/audit_config.py src/taifeng/loop/pool.py src/taifeng/conversation/transcript.py tests/loop/test_audit_capability_gate.py tests/loop/test_audit_engine_integration.py
git commit -m "feat(loop): bootstrap journal audited sessions"
```

### Task 7: Make submission admission Journal-first and lifecycle-safe

**Files:**
- Modify: `src/taifeng/loop/engine.py`
- Modify: `src/taifeng/loop/submission.py`
- Modify: `src/taifeng/loop/pool.py`
- Modify: `tests/loop/test_audit_engine_integration.py`

- [ ] **Step 1: Write failing admission-order tests**

```python
async def test_user_message_is_durable_before_actor_enqueue(audited_engine: AgentEngine) -> None:
    audited_engine.audit.test_pause_after_accept.set()
    submit_task = asyncio.create_task(audited_engine.submit(UserMessage(text="hello")))
    await audited_engine.audit.accepted.wait()
    assert audited_engine.submission_queue_size == 0
    assert _record_types(audited_engine) == [
        "submission_accepted", "conversation_item", "submission_applied"
    ]
    audited_engine.audit.test_pause_after_accept.clear()
    await submit_task


async def test_finishing_rejects_before_accept_and_enqueue(audited_engine: AgentEngine) -> None:
    finish = asyncio.create_task(audited_engine.audit.finish(reason="release"))
    await audited_engine.audit.finishing.wait()
    with pytest.raises(SessionFinishingError):
        await audited_engine.submit(UserMessage(text="late"))
    assert "late" not in _journal_texts(audited_engine)
    assert audited_engine.submission_queue_size == 0
    await finish
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_engine_integration.py -k 'durable_before or finishing' -v`

Expected: tests fail because `AgentEngine.submit()` still enqueues first.

- [ ] **Step 3: Implement accepted submission tokens**

```python
@dataclass(frozen=True)
class AcceptedSubmission:
    submission: Submission
    ack: JournalAck | None = None
    record_ids: tuple[str, ...] = ()


async def submit(self, op: Op) -> str:
    sub = Submission(op=op)
    if self._audit is None:
        await self._submissions.put(AcceptedSubmission(sub))
        return sub.id
    accepted = await self._audit.accept_submission(sub, thread_id=self._thread_id)
    await self._submissions.put(accepted)
    return sub.id
```

The coordinator admission lock performs canonical validation, atomic UserMessage acceptance/conversation/applied commit, and attachment verification before queue insertion. Actor hot history and materialized projection use only the acknowledged conversation item.

- [ ] **Step 4: Implement targeted CancelTurn and unique Shutdown finish**

CancelTurn must use only the target turn token and then commit its applied result. Shutdown competes for `OPEN → FINISHING` under the same lock, closes intake, records the sole accepted Shutdown, cancels the Session root, and asks EnginePool to await the single finish future. Safe emergency Cancel/Shutdown after Journal failure records `audit_complete=false` only in introspection/logging.

- [ ] **Step 5: Run the cancellation/lifecycle matrix**

```bash
PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_engine_integration.py tests/loop/test_cancellation.py -q
```

Expected: accepted-but-queued messages converge; two concurrent turns permit target-only cancellation; concurrent release/Shutdown produces one terminal batch and one close.

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/loop/engine.py src/taifeng/loop/submission.py src/taifeng/loop/pool.py tests/loop/test_audit_engine_integration.py
git commit -m "feat(loop): durably admit audited submissions"
```

### Task 8: Add observed LLM attempts and checkpoint-before-delta

**Files:**
- Create: `src/taifeng/llm/audit.py`
- Modify: `src/taifeng/llm/client.py`
- Modify: `src/taifeng/loop/turn.py`
- Create: `tests/llm/test_audit_attempts.py`

- [ ] **Step 1: Write failing attempt-order tests**

```python
async def test_request_is_durable_before_inner_stream_dispatch() -> None:
    inner = _SpyClient(events=[ResponseEvent.text_delta("hi"), ResponseEvent.completed()])
    observed = AuditedModelClient(inner, observer=_observer())
    await _drain(observed, _request())
    assert observed.trace.index("llm_request_committed") < observed.trace.index("dispatch")


async def test_delta_is_hidden_until_checkpoint_ack() -> None:
    observer = _BlockingCheckpointObserver()
    client = AuditedModelClient(_SpyClient(text="hello"), observer=observer)
    task = asyncio.create_task(_drain(client, _request()))
    await observer.checkpoint_started.wait()
    assert observer.visible_events == []
    observer.release_checkpoint.set()
    await task
    assert _visible_text(observer) == "hello"


async def test_failed_attempt_checkpoint_precedes_retry() -> None:
    client = _RetryingSpyClient(first_error=ServerError("x"), second_text="ok")
    await _drain(AuditedModelClient(client, observer=_observer()), _request())
    assert client.trace == [
        "request:0", "dispatch:0", "checkpoint:error:0",
        "request:1", "dispatch:1", "checkpoint:complete:1",
    ]
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/llm/test_audit_attempts.py -v`

Expected: import failure for `taifeng.llm.audit`.

- [ ] **Step 3: Implement the attempt protocol and decorator**

```python
class ModelAttemptObserver(Protocol):
    async def before_attempt(self, context: ModelAttemptContext) -> ModelAttemptPermit: ...
    async def after_attempt(
        self, permit: ModelAttemptPermit, outcome: ModelAttemptOutcome
    ) -> JournalAck: ...


@runtime_checkable
class AttemptObservableModelClient(ModelClient, Protocol):
    supports_attempt_observer: Literal[True]
```

The decorator allocates retry ordinals, awaits request intent before calling the inner stream, buffers normalized events, awaits the attempt checkpoint, then yields the buffered events in original order. An observer/ack exception freezes the coordinator and prevents another attempt. Current providers make one network attempt per `stream()`; any future internal retry implementation must implement the observer protocol before audit capability validation accepts it.

- [ ] **Step 4: Commit logical response and conversation items in TurnRunner**

After the final complete checkpoint, atomically commit `llm_response_committed` plus ordered reasoning/assistant/function_call conversation items. Apply them to hot history and projection only after ack. Do not append these items through legacy MessageStore in audit mode.

- [ ] **Step 5: Run GREEN and SimClient regression**

```bash
PYTHONPATH=src .venv/bin/pytest tests/llm/test_audit_attempts.py tests/llm/test_sim_client.py tests/llm/test_sim_engine_integration.py -q
.venv/bin/ruff check src/taifeng/llm/audit.py src/taifeng/llm/client.py tests/llm/test_audit_attempts.py
```

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/llm/audit.py src/taifeng/llm/client.py src/taifeng/loop/turn.py tests/llm/test_audit_attempts.py
git commit -m "feat(llm): checkpoint audited model attempts"
```

### Task 9: Converge audited Tool intents and outcomes

**Files:**
- Modify: `src/taifeng/tool/spec.py`
- Modify: `src/taifeng/loop/tool_batch.py`
- Modify: `src/taifeng/loop/turn.py`
- Create: `tests/loop/test_audit_tool_batch.py`

- [ ] **Step 1: Write failing durable-order and cancellation-window tests**

```python
async def test_tool_intent_is_durable_before_dispatch() -> None:
    runtime = _TraceRuntime()
    outcomes = await dispatch_audited_batch(
        [_request("call-1")], runtime=runtime, audit=_audit_trace()
    )
    assert outcomes[0].status == "success"
    assert runtime.trace.index("tool_intent_committed") < runtime.trace.index("dispatch")


@pytest.mark.parametrize(
    ("window", "expected"),
    [("before_dispatch", "cancelled"), ("during_effect", "unknown"),
     ("after_effect", "success")],
)
async def test_tool_cancel_terminal_proof(window: str, expected: str) -> None:
    outcome = await _run_cancel_window(window)
    assert outcome.status == expected
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_tool_batch.py -v`

Expected: failure because audited batching and ToolSpec audit metadata are absent.

- [ ] **Step 3: Add stable ToolSpec audit metadata**

```python
@dataclass(frozen=True)
class ToolSpec:
    effect_kind: Literal["read", "write", "external"] | None = None
    reconciliation: Literal["none", "idempotency_key", "manual"] | None = None
    can_suspend: bool = False
```

Add these fields after the existing non-default fields and keep defaults so legacy constructors remain source-compatible. Populate metadata for built-ins allowed in audit mode. Reject `None` metadata, hooks, permissions, and suspending tools before intent/effect.

- [ ] **Step 4: Replace gather fail-fast with terminal convergence**

Each branch must return one frozen `AuditedToolOutcome`; catch branch exceptions, best-effort cancel siblings, and finalize under a bounded cancellation-independent shield. Sort by request index. Only a pre-dispatch cancellation or an explicit runtime confirmation may become `cancelled`; ambiguous in-effect cancellation/timeout becomes `unknown`.

- [ ] **Step 5: Commit outcomes and outputs atomically**

Commit all `tool_outcome_committed` records and one `conversation_item(function_call_output)` per call in one ordered batch. Reference the unique function_call item created by the LLM response batch; never create another function_call. Any unknown freezes the coordinator before another LLM call.

- [ ] **Step 6: Run GREEN and legacy tool regression**

```bash
PYTHONPATH=src .venv/bin/pytest tests/loop/test_audit_tool_batch.py tests/loop/test_tool_batch.py tests/loop/test_tool_whitelist.py tests/tool/test_cancel_terminal.py -q
```

- [ ] **Step 7: Commit**

```bash
git add src/taifeng/tool/spec.py src/taifeng/loop/tool_batch.py src/taifeng/loop/turn.py tests/loop/test_audit_tool_batch.py
git commit -m "feat(loop): audit tool intent and outcomes"
```

### Task 10: Journal synchronous call_skill lineage and child terminals

**Files:**
- Modify: `src/taifeng/loop/turn.py`
- Modify: `src/taifeng/tool/builtins/call_skill.py`
- Create: `tests/skill/test_audit_call_skill.py`

- [ ] **Step 1: Write failing lineage tests**

```python
async def test_call_skill_commits_child_seed_before_child_turn() -> None:
    result, records = await _run_audited_call_skill()
    assert result.is_error is False
    assert _types(records) == [
        "skill_selected", "skill_dispatch_started", "thread_created",
        "thread_bound", "conversation_item", "turn_started",
        "llm_request_committed", "llm_response_checkpoint",
        "llm_response_committed", "conversation_item",
        "turn_completed", "skill_dispatch_finished", "thread_terminal",
        "conversation_item",
    ]


async def test_quota_rejection_has_finished_without_started() -> None:
    records = await _run_quota_rejected_call_skill()
    finished = _one(records, "skill_dispatch_finished")
    assert finished.payload["status"] == "rejected"
    assert finished.payload["started_record_id"] is None
    assert "thread_created" not in _types(records)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=src .venv/bin/pytest tests/skill/test_audit_call_skill.py -v`

Expected: existing call_skill uses legacy transcript writes and produces no Journal lineage.

- [ ] **Step 3: Implement selection, quota, and child bootstrap records**

After outer tool intent ack, commit full skill snapshot in `skill_selected`. On quota acceptance preallocate the child thread id and atomically commit started/thread-created/thread-bound/child-seed. Apply the child seed to hot child history after ack; projection failure only marks stale.

- [ ] **Step 4: Implement child finish atomicity**

Use `{child_thread_id}:{parent_submission_id}:turn:{turn_index}`. On child success/error/cancel atomically commit `skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)`, then let the outer tool batch commit its outcome/output. Reject any SuspendSignal as a declared-capability violation before HITL effect and freeze if a supposedly non-suspending implementation violates the declaration.

- [ ] **Step 5: Run three-level lineage and regression tests**

```bash
PYTHONPATH=src .venv/bin/pytest tests/skill/test_audit_call_skill.py tests/skill/test_composite_e2e.py tests/skill/test_dispatch.py tests/loop/test_skill_outcome_wiring.py -q
```

- [ ] **Step 6: Commit**

```bash
git add src/taifeng/loop/turn.py src/taifeng/tool/builtins/call_skill.py tests/skill/test_audit_call_skill.py
git commit -m "feat(skill): audit call_skill lineage"
```

### Task 11: Close dynamic capability gaps and prove the end-to-end slice

**Files:**
- Modify: `src/taifeng/loop/audit_config.py`
- Modify: `src/taifeng/loop/engine.py`
- Modify: `src/taifeng/loop/turn.py`
- Modify: `tests/loop/test_audit_capability_gate.py`
- Modify: `tests/loop/test_audit_engine_integration.py`

- [ ] **Step 1: Add one test per unsupported dynamic path**

Parameterize and assert no effect spy calls for unsupported Op, resume, custom store/directory, IndexHook, hooks, permission, HITL, compressor, memory, instruction update, orchestration, detached spawn, peer, suspension, and unobserved model client.

```python
@pytest.mark.parametrize("op", [CompactNow(), Rewind(), SpawnSkill(...)])
async def test_unsupported_op_is_rejected_before_effect(
    audited_engine: AgentEngine, op: Op
) -> None:
    with pytest.raises(AuditCapabilityError):
        await audited_engine.submit(op)
    assert audited_engine.effect_spy.calls == []
```

- [ ] **Step 2: Add audited marker downgrade protection**

Create a default transcript carrying `audit_required=true`; a later non-audit `get_or_create(resume_thread_id=...)` must raise `AuditDowngradeError` before history is used or an actor starts.

- [ ] **Step 3: Add complete success/error/cancel/freeze scenarios**

Use SimClient and deterministic fake tools to assert exact record sequences for plain assistant completion, basic tool completion, call_skill, provider error, tool error, targeted cancel, Journal intent failure, outcome failure/UNKNOWN, projection failure, and two-Session isolation.

- [ ] **Step 4: Run focused integration and full unit suite**

```bash
PYTHONPATH=src .venv/bin/pytest tests/conversation/journal tests/loop/test_audit_coordinator.py tests/loop/test_audit_capability_gate.py tests/loop/test_audit_engine_integration.py tests/loop/test_audit_tool_batch.py tests/llm/test_audit_attempts.py tests/skill/test_audit_call_skill.py -q
PYTHONPATH=src .venv/bin/pytest tests/ -q
```

Expected: all new audit tests and the existing suite pass.

- [ ] **Step 5: Commit**

```bash
git add src/taifeng/loop/audit_config.py src/taifeng/loop/engine.py src/taifeng/loop/turn.py tests/loop/test_audit_capability_gate.py tests/loop/test_audit_engine_integration.py
git commit -m "test(loop): verify audited business execution"
```

### Task 12: Synchronize living architecture and pass repository gates

**Files:**
- Modify: `docs/architecture/conversation.md`
- Modify: `docs/architecture/agent-loop.md`
- Modify: `docs/architecture/llm-client.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/real-llm-ledger.json`
- Modify: `docs/real-llm-ledger.md`
- Modify: `openspec/changes/add-session-journal-business-integration/tasks.md`

- [ ] **Step 1: Update living architecture**

Document the delivered current-state data flow, the strict audit capability matrix, Session fail-closed scope, MessageStore materialization boundary, LLM attempt observer, Tool/Skill UNKNOWN semantics, and the explicit absence of open/recovery/HITL/compaction/spawn support.

- [ ] **Step 2: Register the LLM strategy capability**

Add a `SessionJournal checkpoint-before-delta` row to `docs/capability-matrix.md` with Sim coverage, real-LLM scenario ids, and the fact that audit mode buffers deltas until checkpoint ack.

- [ ] **Step 3: Run static and simulated gates**

```bash
git diff --check
.venv/bin/ruff check src/taifeng/conversation/journal/records.py src/taifeng/conversation/journal/projector.py src/taifeng/loop/audit.py src/taifeng/loop/audit_config.py src/taifeng/llm/audit.py tests/conversation/journal/test_records.py tests/conversation/journal/test_projector.py tests/loop/test_audit_coordinator.py tests/loop/test_audit_capability_gate.py tests/loop/test_audit_engine_integration.py tests/loop/test_audit_tool_batch.py tests/llm/test_audit_attempts.py tests/skill/test_audit_call_skill.py
PYTHONPATH=src .venv/bin/mypy src/taifeng
PYTHONPATH=src .venv/bin/pytest tests/ -q
PYTHONPATH=src .venv/bin/python examples/real_llm/selfcheck.py
openspec validate add-session-journal-business-integration --strict
```

Expected: diff check, focused Ruff, full mypy, full pytest, Sim selfcheck, and strict OpenSpec validation all pass.

- [ ] **Step 4: Run the mandatory real-LLM capability matrix**

Run only with the user's informed external-provider authorization:

```bash
PYTHONPATH=src .venv/bin/python examples/real_llm/capability_matrix.py
```

Expected: all required real-provider scenarios pass and both `docs/real-llm-ledger.json` and `.md` are regenerated at the final code commit. Without this result, do not mark the OpenSpec task complete and do not archive/merge.

- [ ] **Step 5: Complete tasks, review, and commit**

Mark only evidenced OpenSpec checkboxes complete, run an independent code/spec review, and fix all Critical/Important findings. Then:

```bash
git add docs/architecture docs/capability-matrix.md docs/real-llm-ledger.json docs/real-llm-ledger.md openspec/changes/add-session-journal-business-integration
git commit -m "docs: complete journal business integration"
```

Do not archive or merge unless the user explicitly requests it.
