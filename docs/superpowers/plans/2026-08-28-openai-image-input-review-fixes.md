# OpenAI 图片输入审查修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复第三方审查确认的图片输入、OpenAI Chat/Responses、默认 JSONL 恢复与敏感数据边界问题，使本地实现达到可合并标准。

**Architecture:** 保持已批准的双协议和默认禁用图片策略不变。协议边界统一在网络前验证；默认 JSONL 用跨 writer 文件锁保证原子 ack，并在冷恢复时以稳定错误 output 收敛结果未知的 Responses tool call，绝不自动重放可能有副作用的工具。图片解析、token 估值和可取消 SSE 各自保持为小型纯函数或共享迭代器。

**Tech Stack:** Python 3.12+、Pydantic、anyio/asyncio、httpx、pytest、JSONL。

---

### Task 1: 敏感请求和 Responses 协议前置校验

**Files:**
- Modify: `src/taifeng/llm/image_input.py`
- Modify: `src/taifeng/llm/audit.py`
- Modify: `src/taifeng/loop/turn.py`
- Modify: `src/taifeng/loop/prompt.py`
- Modify: `src/taifeng/llm/providers/openai/responses.py`
- Test: `tests/llm/test_image_input_types.py`
- Test: `tests/llm/test_openai_responses.py`
- Test: `tests/llm/test_audit_attempt_checkpoint.py`
- Test: `tests/loop/test_prompt_image_input.py`

- [ ] **Step 1: 写入失败测试**

```python
def test_sensitive_request_redaction_removes_image_and_encrypted_state():
    redacted = redact_sensitive_request_data(request)
    encoded = json.dumps(redacted)
    assert image_base64 not in encoded
    assert encrypted_state not in encoded
    assert "encrypted_content" not in encoded

def test_responses_does_not_force_strict_for_arbitrary_tool_schema():
    payload = session._build_payload(request_with_call_skill_schema)
    assert "strict" not in payload["tools"][0]

def test_provider_state_rejected_when_client_does_not_accept_it():
    with pytest.raises(InvalidHistoryError):
        build_api_request(history_with_provider_state, model_input_capabilities=text_only)

def test_terminal_function_call_requires_non_empty_identity():
    with pytest.raises(InvalidResponseError):
        accumulator.finalize(response_with_empty_call_id)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_image_input_types.py tests/llm/test_openai_responses.py tests/llm/test_audit_attempt_checkpoint.py tests/loop/test_prompt_image_input.py -q`

Expected: 新增断言分别因密文保留、`strict:true`、provider state 静默投影和空 call id 被接受而失败。

- [ ] **Step 3: 实施最小修复**

```python
def redact_sensitive_request_data(value: object) -> Any:
    if isinstance(value, list):
        return [redact_sensitive_request_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image" and "base64_data" in value:
        return image_descriptor(value)
    redacted = {
        key: redact_sensitive_request_data(item)
        for key, item in value.items()
        if key != "encrypted_content"
    }
    if "encrypted_content" in value:
        redacted["provider_state_redacted"] = True
    return redacted
```

在 request capture 和 `ModelAttemptRequest.api_request` 两处调用统一脱敏函数；`build_api_request` 在存在 provider state 且 `accepts_provider_state=False` 时抛 `InvalidHistoryError`；Responses tool 不再强制发送 `strict:true`；`NormalizedFunctionCallItem.call_id/name` 使用 `Field(min_length=1)`。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_image_input_types.py tests/llm/test_openai_responses.py tests/llm/test_audit_attempt_checkpoint.py tests/loop/test_prompt_image_input.py -q`

Expected: PASS。

- [ ] **Step 5: 提交切片**

```bash
git add src/taifeng/llm/image_input.py src/taifeng/llm/audit.py src/taifeng/loop/turn.py src/taifeng/loop/prompt.py src/taifeng/llm/providers/openai/responses.py tests/llm tests/loop/test_prompt_image_input.py
git commit -m "fix: close OpenAI request safety boundaries"
```

### Task 2: JSONL 跨 writer 原子性和 Responses 工具冷恢复

**Files:**
- Modify: `src/taifeng/conversation/transcript.py`
- Modify: `src/taifeng/loop/pool_session.py`
- Modify: `src/taifeng/loop/event.py`
- Test: `tests/conversation/test_atomic_response_batches.py`
- Test: `tests/loop/test_openai_image_cold_resume.py`

- [ ] **Step 1: 写入失败测试**

```python
async def test_atomic_batch_conflict_is_serialized_across_writer_instances(tmp_path):
    first, second = JsonlMessageStore(tmp_path), JsonlMessageStore(tmp_path)
    results = await asyncio.gather(
        first.append_atomic_batch([item_a], batch_id="same"),
        second.append_atomic_batch([item_b], batch_id="same"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, BatchAppendAck) for value in results) == 1
    assert sum(isinstance(value, BatchConflictError) for value in results) == 1

async def test_cold_resume_settles_orphan_response_call_as_unknown_without_dispatch(...):
    await store.append_atomic_batch([call], batch_id=sample_id)
    engine = await pool.get_or_create(..., resume_thread_id=thread_id)
    history = engine.history_snapshot()
    assert history[-1].kind == "function_call_output"
    assert history[-1].payload["is_error"] is True
    assert dispatch_count == 0
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_atomic_response_batches.py tests/loop/test_openai_image_cold_resume.py -q`

Expected: 两个 writer 均成功；冷恢复仍留下孤立 call。

- [ ] **Step 3: 实施跨 writer 排他**

```python
@asynccontextmanager
async def _exclusive_file_lock(path: Path):
    handle = await anyio.to_thread.run_sync(_open_lock_file, path)
    try:
        await anyio.to_thread.run_sync(_lock_exclusive, handle)
        yield
    finally:
        await anyio.to_thread.run_sync(_unlock_and_close, handle)
```

所有默认 JSONL `append`/`append_atomic_batch` 使用同一 `<thread>.lock`，把 committed 检查和 durable append 放入同一跨进程临界区；保留实例内 anyio lock。

- [ ] **Step 4: 实施未知工具结果收敛**

```python
def _unknown_tool_output(call: ResponseItem) -> ResponseItem:
    return ResponseItem(
        kind="function_call_output",
        id=stable_recovery_item_id(call),
        thread_id=call.thread_id,
        created_at=call.created_at,
        payload={
            "call_id": call.payload["call_id"],
            "output": "tool outcome unknown after process recovery; not retried",
            "is_error": True,
        },
        metadata={"origin_llm_sample_id": call.metadata["llm_sample_id"]},
    )
```

仅处理带 `llm_sample_id`、没有 matching output、且不属于活跃 suspension 的 Responses call；按 sample 用稳定 recovery batch id 原子提交。恢复不执行工具，并在 `thread_resumed` 数据中暴露 call ids。

- [ ] **Step 5: 运行定向测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_atomic_response_batches.py tests/loop/test_openai_image_cold_resume.py -q`

Expected: PASS；重复恢复不增加 output。

- [ ] **Step 6: 提交切片**

```bash
git add src/taifeng/conversation/transcript.py src/taifeng/loop/pool_session.py src/taifeng/loop/event.py tests/conversation/test_atomic_response_batches.py tests/loop/test_openai_image_cold_resume.py
git commit -m "fix: make Responses recovery crash safe"
```

### Task 3: 图片格式解析与 GPT-5.6 token 估值

**Files:**
- Modify: `src/taifeng/llm/image_input.py`
- Modify: `src/taifeng/loop/engine.py`
- Test: `tests/llm/test_image_admission.py`
- Test: `tests/loop/test_image_input_wiring.py`

- [ ] **Step 1: 写入失败测试**

```python
def test_static_gif_comment_comma_is_not_a_second_frame(): ...
def test_webp_vp8_and_vp8l_dimensions_are_supported(): ...
def test_animated_vp8x_is_rejected(): ...
def test_gpt_56_1024_square_high_costs_1229_tokens():
    assert estimator.estimate_image_tokens(..., width=1024, height=1024, detail="high") == 1229
def test_engine_estimate_tokens_uses_injected_image_estimator(): ...
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_image_admission.py tests/loop/test_image_input_wiring.py -q`

Expected: GIF/WebP、1229 token 和公共估值一致性断言失败。

- [ ] **Step 3: 实施可靠 header 解析和 patch 估算**

```python
patches = ceil(resized_width / 32) * ceil(resized_height / 32)
tokens = ceil(patches * 1.2)
```

GIF 按 block/sub-block 结构遍历 image descriptor；WebP 分别解析 `VP8X`、`VP8 `、`VP8L`，并通过 VP8X animation flag 拒绝动画；GPT-5.6 low/high/original/auto 分别应用官方 512、2048+2500 patches、65535/no-budget 规则。`AgentEngine.estimate_tokens()` 传入与 TurnRunner 相同的 policy、estimator 和 entry model。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_image_admission.py tests/loop/test_image_input_wiring.py -q`

Expected: PASS。

- [ ] **Step 5: 提交切片**

```bash
git add src/taifeng/llm/image_input.py src/taifeng/loop/engine.py tests/llm/test_image_admission.py tests/loop/test_image_input_wiring.py
git commit -m "fix: align image parsing and GPT-5.6 budgeting"
```

### Task 4: Chat 终态与阻塞 SSE 取消

**Files:**
- Modify: `src/taifeng/llm/providers/_shared.py`
- Modify: `src/taifeng/llm/providers/openai_compat.py`
- Modify: `src/taifeng/llm/providers/openai/responses.py`
- Test: `tests/llm/test_openai_chat.py`
- Test: `tests/llm/test_openai_responses.py`

- [ ] **Step 1: 写入失败测试**

```python
async def test_chat_clean_eof_without_done_or_finish_reason_fails(): ...
async def test_chat_stalled_read_is_interrupted_by_cancel_token(): ...
async def test_responses_stalled_read_is_interrupted_by_cancel_token(): ...
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_openai_chat.py tests/llm/test_openai_responses.py -q`

Expected: Chat 发出 completed；两个 stalled stream 在测试 deadline 内不退出。

- [ ] **Step 3: 实施共享可取消行迭代器和 Chat terminal gate**

```python
async def iter_lines_with_cancel(response, cancel):
    iterator = response.aiter_lines().__aiter__()
    while True:
        line_task = asyncio.create_task(anext(iterator))
        cancel_task = asyncio.create_task(cancel.wait_cancelled())
        done, _ = await asyncio.wait({line_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done:
            line_task.cancel()
            await asyncio.gather(line_task, return_exceptions=True)
            cancel.raise_if_cancelled()
        cancel_task.cancel()
        try:
            yield line_task.result()
        except StopAsyncIteration:
            return
```

Chat 仅在观察到 `[DONE]` 或非空 `finish_reason` 后允许 completed；Responses 继续要求 `response.completed`。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_openai_chat.py tests/llm/test_openai_responses.py -q`

Expected: PASS。

- [ ] **Step 5: 提交切片**

```bash
git add src/taifeng/llm/providers/_shared.py src/taifeng/llm/providers/openai_compat.py src/taifeng/llm/providers/openai/responses.py tests/llm/test_openai_chat.py tests/llm/test_openai_responses.py
git commit -m "fix: fail closed on interrupted OpenAI streams"
```

### Task 5: 架构同步和完整验证

**Files:**
- Modify: `docs/architecture/capabilities/llm-image-input.md`
- Modify: `docs/architecture/conversation.md`
- Modify: `docs/architecture/llm-client.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/real-llm-ledger.json`
- Modify: `docs/real-llm-ledger.md`

- [ ] **Step 1: 同步活文档**

记录敏感 request descriptor、Responses 非强制 strict、provider-state gate、unknown tool outcome 冷恢复、跨 writer 原子锁、图片格式解析、GPT-5.6 patch 估值、Chat terminal 与可取消 SSE。

- [ ] **Step 2: 运行定向回归**

Run: `PYTHONPATH=src uv run pytest tests/llm tests/conversation/test_atomic_response_batches.py tests/loop/test_image_input_wiring.py tests/loop/test_openai_image_cold_resume.py tests/loop/test_prompt_image_input.py tests/loop/test_openai_responses_durable.py -q`

Expected: PASS。

- [ ] **Step 3: 运行全量和 selfcheck**

```bash
PYTHONPATH=src uv run pytest tests/ -q
PYTHONPATH=src uv run python examples/real_llm/selfcheck.py
git diff --check 245181acb18a606dbb55f2e2a202a43b9154c628..HEAD
```

Expected: 全部 exit 0。

- [ ] **Step 4: 执行真实 GPT-5.6 矩阵或保持 NOT_EXECUTED**

Run when an authorized key is present: `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --provider openai --model gpt-5.6`

Expected: 有凭据时更新 PASS/FAIL 台账；无凭据时保留 `NOT_EXECUTED`，不把 selfcheck 当真实验收。

- [ ] **Step 5: 提交文档和验证台账**

```bash
git add docs/architecture docs/capability-matrix.md docs/real-llm-ledger.json docs/real-llm-ledger.md
git commit -m "docs: record hardened OpenAI image boundaries"
```

## Plan self-review

- 覆盖审查中的 3 个 Critical 和 8 个 Important；不引入音频、URL、Files API 或 hosted tools。
- 所有生产行为先有能复现原问题的失败测试。
- crash recovery 不自动重放工具副作用，只以稳定 error output 明确收敛未知结果。
- OpenAI strict 和图片 token 规则以 2026-08-28 官方文档为准。
- 真实 GPT-5.6 矩阵仍受授权凭据约束，验证层级保持独立。
