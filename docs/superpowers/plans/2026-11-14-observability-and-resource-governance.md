# 资源治理与真实回归观测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为短命存储增加资源边界，为真实回归增加状态分类，并让执行树明确报告用量完整性原因。

**Architecture:** 保持三条能力边界独立：存储在 `conversation/store.py` 做写前容量检查；台账在 `examples/real_llm/_ledger.py` 做向后兼容分类；执行观测在 `execution_observer.py` 生成事实字段，终端投影只消费已有完整度语义。每个边界先写契约测试，再改实现。

**Tech Stack:** Python 3.12+, Pydantic, pytest/pytest-asyncio, JSONL ledger, existing EventMsg projection.

---

### Task 1: 短命存储资源边界

**Files:**
- Modify: `src/taifeng/conversation/store.py`
- Test: `tests/conversation/test_ephemeral_store.py`
- Modify: `docs/architecture/capabilities/ephemeral-message-store.md`
- Modify: `docs/architecture/conversation.md`

- [ ] **Step 1: Write failing tests** for the four default limits, thread/item/byte limits, atomic idempotency before capacity checks, cross-thread aggregate limits, and close counter reset.
- [ ] **Step 2: Run the focused tests and verify the new assertions fail because limits do not exist.**
- [ ] **Step 3: Add `EphemeralStoreLimits`, `EphemeralStoreLimitError`, counters, and write-before-capacity checks.**
- [ ] **Step 4: Run the focused tests and the existing ephemeral-store tests.**
- [ ] **Step 5: Update the capability contract and commit.**

### Task 2: Real regression status classification

**Files:**
- Modify: `examples/real_llm/_ledger.py`
- Modify: `examples/real_llm/capability_matrix.py`
- Test: `tests/llm/test_real_llm_ledger_status.py`
- Modify: `docs/real-llm-ledger.md` only through the generator if a real run is performed.

- [ ] **Step 1: Write failing tests** for stable `failure_class` and legacy note classification, transient provider errors, code failures, old records without status, and NOT_EXECUTED rendering.
- [ ] **Step 2: Run the focused tests and verify the classifier is absent.**
- [ ] **Step 3: Add status classification and preserve the existing verdict field.**
- [ ] **Step 4: Run ledger and capability-matrix simulator tests.**
- [ ] **Step 5: Commit the classifier and tests.**

### Task 3: Execution usage completeness facts

**Files:**
- Modify: `src/taifeng/loop/execution_observer.py`
- Test: `tests/examples/test_execution_tree_verification.py`
- Modify: `docs/architecture/capabilities/execution-observability.md`

- [ ] **Step 1: Write failing tests** requiring attempt, metered, and missing usage counts on execution updates and terminal events, complete-only metering, provider retry, cancellation, suspension, and engine gate terminals.
- [ ] **Step 2: Run the focused tests and verify the fields are absent.**
- [ ] **Step 3: Add the count fields without relaxing `usage_complete`.**
- [ ] **Step 4: Run execution-tree tests and projection tests.**
- [ ] **Step 5: Commit the observation contract.**

### Task 4: Integrated verification

- [ ] **Step 1: Run focused tests for all three tasks.**
- [ ] **Step 2: Run full pytest on Python 3.12 and 3.13, examples, ruff, mypy, and uv lock check.**
- [ ] **Step 3: Run the real Codex capability matrix and record its status classification.**
- [ ] **Step 4: Merge into main, rerun the full gates, and clean the worktree and branch.**
