# Independent Codex Responses Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增显式 `provider=codex` 的独立 Responses/Codex 客户端，兼容顶层 instructions、typed input list、done-item 终态、图片输入和隔离的 reasoning state，同时保持 OpenAI clients 行为不变。

**Architecture:** provider-neutral normalized response items 保持 Loop 的唯一投影类型；Codex 自己拥有 wire builder、SSE 状态机和 terminal accumulator，只复用 HTTP/error/usage/cancellation 等纯工具。strict request intent 升级为 V2 安全投影 + manifest + canonical digest，Codex/OpenAI state 在网络前 exact-match 隔离。

**Tech Stack:** Python 3.12、Pydantic v2、anyio、httpx/SSE、pytest、JSONL SessionJournal、Sim/真实 capability matrix。

---

## 文件结构

| 路径 | 职责 |
| --- | --- |
| `src/taifeng/llm/responses_types.py` | provider-neutral normalized reasoning/message/function-call/refusal DTO。 |
| `src/taifeng/llm/providers/codex/wire.py` | Codex instructions、typed input、tool、format、state 和 request bytes。 |
| `src/taifeng/llm/providers/codex/accumulator.py` | Codex SSE output/content-part 配对、done-item/completed 一致性。 |
| `src/taifeng/llm/providers/codex/responses.py` | 单 HTTP attempt、取消、错误/usage、公开 client/session。 |
| `src/taifeng/llm/audit_redaction.py` | request V2 安全投影、RFC 6901 manifest、RFC 8785 digest。 |
| `src/taifeng/conversation/journal/records.py` | `RedactionEntryV1`、`LlmRequestCommittedV2`、V1/V2 decoder。 |
| `examples/_provider_bootstrap.py` | `provider=codex` 配置真值表、URL 校验和 metadata。 |
| `examples/real_llm/test_codex_image_matrix.py` | Codex 纯文本、图片、tool、state 热/冷路径真实验收。 |

### Task 1: Provider-neutral normalized items

**Files:**
- Create: `src/taifeng/llm/responses_types.py`
- Modify: `src/taifeng/llm/providers/openai/responses.py`, `src/taifeng/loop/turn.py`
- Test: `tests/llm/test_responses_types.py`, `tests/llm/test_openai_responses.py`

- [ ] **Step 1: 写失败导入与 strict DTO 测试。**

```python
def test_normalized_function_call_rejects_empty_identity() -> None:
    with pytest.raises(ValidationError):
        NormalizedFunctionCallItem(output_index=0, call_id="", name="inspect", arguments="{}")
```

- [ ] **Step 2: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_responses_types.py -q`

Expected: FAIL，因为 `taifeng.llm.responses_types` 尚不存在。

- [ ] **Step 3: 移动 frozen/extra-forbid DTO，不改变 OpenAI accumulator。** `responses_types.py` 定义 `_NormalizedItem`、`NormalizedReasoningItem`、`NormalizedMessageItem`、`NormalizedFunctionCallItem`、`NormalizedRefusalItem` 和 union；OpenAI 文件 import/re-export；Loop 改从中性模块导入。

- [ ] **Step 4: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_responses_types.py tests/llm/test_openai_responses.py -q`

Expected: PASS，OpenAI 既有测试无行为变化。

```bash
git add src/taifeng/llm/responses_types.py src/taifeng/llm/providers/openai/responses.py src/taifeng/loop/turn.py tests/llm/test_responses_types.py
git commit -m "refactor: share normalized Responses item types"
```

### Task 2: Strict request-intent V2 data minimization

**Files:**
- Create: `src/taifeng/llm/audit_redaction.py`, `tests/llm/test_audit_redaction_v2.py`
- Modify: `src/taifeng/llm/audit.py`, `src/taifeng/loop/audit_llm.py`, `src/taifeng/conversation/journal/records.py`, `tests/conversation/journal/test_records.py`

- [ ] **Step 1: 写失败 vector、manifest、collision 和 V1/V2 reader 测试。**

```python
def test_attempt_digest_matches_contract_vector() -> None:
    projection = project_attempt_request("codex", "gpt-5.6-luna", PING_REQUEST)
    assert projection.canonical_attempt_sha256 == "ca2f8ff5fcb8a45b8725d71e1943da15346e5ae2006adc6232e4b1cbd8fc13eb"

def test_redactions_use_sorted_unique_json_pointers() -> None:
    projection = project_attempt_request("codex", "m", IMAGE_AND_STATE_REQUEST)
    assert [entry.path for entry in projection.redactions] == sorted(
        {entry.path for entry in projection.redactions}, key=lambda value: value.encode("utf-8")
    )
```

- [ ] **Step 2: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_audit_redaction_v2.py tests/conversation/journal/test_records.py -q`

Expected: FAIL，因为 projector 和 V2 DTO 尚不存在。

- [ ] **Step 3: 实现 fail-closed projector。** 用 `ApiRequest.model_dump(mode="json")` 构造完整树；图片删除 `base64_data` 并加入 `content_redacted={"kind":"image_base64","redacted":True}`；state 删除 `encrypted_content` 并加入 provider marker；生成 RFC 6901 path、UTF-8 bytes 排序、重复拒绝；对 `{provider,model,api_request}` 的 `canonical_bytes()` 计算 SHA-256。

- [ ] **Step 4: 实现 `LlmRequestCommittedV2` 与 reader 路由。** V1 保留只读；writer/observer 只产生 V2 字段 `api_request_safe/redactions/canonical_attempt_sha256`。`ModelAttemptRequest` 携不可变安全投影和 digest，不把正文交给 observer。

- [ ] **Step 5: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_audit_redaction_v2.py tests/llm/test_audit_attempt_checkpoint.py tests/conversation/journal/test_records.py -q`

Expected: PASS，sentinel base64/ciphertext 不出现在 observer/Journals request intent。

```bash
git add src/taifeng/llm/audit_redaction.py src/taifeng/llm/audit.py src/taifeng/loop/audit_llm.py src/taifeng/conversation/journal/records.py tests/llm/test_audit_redaction_v2.py tests/conversation/journal/test_records.py
git commit -m "feat: minimize sensitive LLM request intents"
```

### Task 3: Codex request wire and provider-state isolation

**Files:**
- Create: `src/taifeng/llm/providers/codex/__init__.py`, `src/taifeng/llm/providers/codex/wire.py`, `tests/llm/test_codex_wire.py`
- Modify: `src/taifeng/conversation/journal/records.py`, `tests/llm/test_openai_responses.py`

- [ ] **Step 1: 写失败 canonical wire tests。** 覆盖空 prompt 过滤、空白 prompt 原样保留、`\n\n` join、无 system item、input 恒 list、单图/多图、flat tools、无 tool strict、parallel setting、function output、`text.format`、request bytes 和非法 role。

```python
def test_codex_uses_top_level_instructions_and_list_input() -> None:
    payload = build_codex_payload(REQUEST_WITH_SYSTEM)
    assert payload["instructions"] == " first \n\nsecond"
    assert isinstance(payload["input"], list)
    assert all(item.get("role") != "system" for item in payload["input"])
```

- [ ] **Step 2: 写双向 state 隔离红测。** Codex 只接受 exact codex/responses/reasoning + 五键白名单；OpenAI 拒 Codex；strict Journal 同样验证两种 provider payload。

- [ ] **Step 3: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_wire.py tests/llm/test_openai_responses.py -q`

Expected: FAIL，因为 Codex wire 模块不存在。

- [ ] **Step 4: 实现纯 builder。** 复用 image Data URL 与最终 UTF-8 byte guard，不导入/修改 OpenAI client 分支；state serializer 接收显式 `provider="codex"`；endpoint 不在 builder 内推断。

- [ ] **Step 5: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_wire.py tests/llm/test_openai_responses.py tests/conversation/journal/test_records.py -q`

Expected: PASS。

```bash
git add src/taifeng/llm/providers/codex src/taifeng/conversation/journal/records.py tests/llm/test_codex_wire.py tests/llm/test_openai_responses.py
git commit -m "feat: build isolated Codex Responses requests"
```

### Task 4: Codex done-item SSE state machine

**Files:**
- Create: `src/taifeng/llm/providers/codex/accumulator.py`, `tests/llm/test_codex_accumulator.py`

- [ ] **Step 1: 写成功路径红测。** 真实探针顺序必须收敛 done message；completed.output 空时用 done；非空 output 用 position/index exact canonical comparison；输出至少一项。

```python
def test_done_items_are_fact_source_when_completed_output_is_empty() -> None:
    accumulator = CodexResponsesAccumulator()
    for event in TEXT_DONE_SEQUENCE:
        accumulator.accept(event)
    result = accumulator.complete(COMPLETED_WITH_EMPTY_OUTPUT)
    assert result.items[0].text == "库存 A-17"
```

- [ ] **Step 2: 写 fail-closed 红测。** 覆盖 index 非 0/跳号/倒序/重复、delta-before-added、identity drift、done 后事件、content part 缺配对、delta mismatch、hosted tool、零 done、completed 缺 id/status/usage、usage bool/负数/total mismatch、重复 completed、completed 后 event、EOF。

- [ ] **Step 3: 写 refusal 红测。** 非空 refusal → `ContentFilterError`；空 refusal → `InvalidResponseError`；refusal/output_text 混合拒绝。

- [ ] **Step 4: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_accumulator.py -q`

Expected: FAIL，因为 accumulator 不存在。

- [ ] **Step 5: 实现状态机。** 每个 output index 保存 added identity、delta buffers、content parts、done item；`response.completed` 只保存候选 terminal，clean EOF 才 finalize；只 allow reasoning/message/function_call 和 output_text/refusal；usage 用 strict non-bool integer validator。

- [ ] **Step 6: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_accumulator.py -q`

Expected: PASS。

```bash
git add src/taifeng/llm/providers/codex/accumulator.py tests/llm/test_codex_accumulator.py
git commit -m "feat: validate Codex done-item streams"
```

### Task 5: Codex network session, cancellation and audit allowlist

**Files:**
- Create: `src/taifeng/llm/providers/codex/responses.py`, `tests/llm/test_codex_responses.py`
- Modify: `src/taifeng/llm/audit.py`

- [ ] **Step 1: 写 mock transport 红测。** 断言 POST `<base_url>/responses`、200 SSE、`normalized_output` 恰好一次且在 completed 前、usage/cache/request-id；HTTP errors 使用既有分类；stalled read 被 token 在 1 秒内取消并关闭。

- [ ] **Step 2: 写 terminal buffering 红测。** completed 后额外 JSON event 必须失败且不产生 normalized/completed；clean EOF 才发布 terminal；取消只允许之前 preview，不允许 durable terminal。

- [ ] **Step 3: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_responses.py -q`

Expected: FAIL，因为 session/client 尚不存在。

- [ ] **Step 4: 实现 one-network-attempt session/client。** 使用 `iter_lines_with_cancel` 和严格 Codex SSE parser；client capabilities 固定 `provider=codex/protocol=responses/text+image/state`，默认 model 为 `gpt-5.6-luna`；内部不 retry。

- [ ] **Step 5: 将 exact `CodexResponsesClient` 加入 strict audit allowlist 并测试外部 subclass 仍被拒。**

- [ ] **Step 6: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_responses.py tests/llm/test_audit_attempt_adapter.py -q`

Expected: PASS。

```bash
git add src/taifeng/llm/providers/codex/responses.py src/taifeng/llm/audit.py tests/llm/test_codex_responses.py tests/llm/test_audit_attempt_adapter.py
git commit -m "feat: add Codex Responses network client"
```

### Task 6: Bootstrap, exports and Loop integration

**Files:**
- Modify: `examples/_provider_bootstrap.py`, `.env.example`, `tests/test_provider_bootstrap.py`, `src/taifeng/llm/providers/__init__.py`, `src/taifeng/llm/__init__.py`, `src/taifeng/__init__.py`, `tests/loop/test_openai_image_cold_resume.py`

- [ ] **Step 1: 写 bootstrap 真值表红测。** Codex protocol 缺失/responses 接受；chat/response 拒绝；base URL 缺失、非 HTTP(S)、userinfo/query/fragment、尾部 responses 拒绝；trailing slash 规范化；legacy OpenAI env 不得流入 Codex；`require_api_key=False` 不产生 tail；metadata 含 dialect。

- [ ] **Step 2: 写公开导入和 sample identity 红测。** `taifeng.CodexResponsesClient` 与 provider import 同一类型；legacy/strict Codex normalized items 都按 `(thread,submission,turn,iteration)` 写同一稳定 `llm_sample_id`。

- [ ] **Step 3: 运行红灯。**

Run: `PYTHONPATH=src uv run pytest tests/test_provider_bootstrap.py tests/llm/test_public_imports.py tests/loop/test_openai_image_cold_resume.py -q`

Expected: FAIL，因为 bootstrap/export 尚未登记 Codex。

- [ ] **Step 4: 实现 URL parser、client construction、metadata 和公共 exports。** `.env.example` 同时给 OpenAI 与 Codex 二选一示例，不放真实域名/key；OpenAI 默认和旧变量行为保持不变。

- [ ] **Step 5: 验证并提交。**

Run: `PYTHONPATH=src uv run pytest tests/test_provider_bootstrap.py tests/llm/test_public_imports.py tests/loop/test_openai_image_cold_resume.py -q`

Expected: PASS。

```bash
git add examples/_provider_bootstrap.py .env.example src/taifeng/__init__.py src/taifeng/llm tests/test_provider_bootstrap.py tests/llm/test_public_imports.py tests/loop/test_openai_image_cold_resume.py
git commit -m "feat: bootstrap independent Codex provider"
```

### Task 7: Sim preflight, real Codex matrix and living docs

**Files:**
- Create: `examples/real_llm/test_codex_image_matrix.py`
- Modify: `examples/real_llm/selfcheck.py`, `examples/real_llm/capability_matrix.py`, `examples/real_llm/_ledger.py`, `docs/architecture/llm-client.md`, `docs/architecture/agent-loop.md`, `docs/architecture/conversation.md`, `docs/capability-matrix.md`

- [ ] **Step 1: 添加零消耗 preflight。** 用 mock Codex event fixtures 验证 instructions/list input、单图/多图 wire、done→completed、state replay 和所有 capture/observer sentinel 脱敏。

- [ ] **Step 2: 添加真实矩阵。** `provider=codex` 时运行纯文本 instructions、单图、多图顺序、图片驱动 tool、encrypted state 热重放与 legacy JSONL 冷恢复；每场必须检查非零合法 usage、语义断言、provider identity、无敏感正文写到 logs/capture。

- [ ] **Step 3: 更新 ledger merge 规则和活文档。** Codex 场景前缀 `codex_`，真实未运行保留 `NOT_EXECUTED`；能力矩阵真实验证列指向新场景；OpenAI 行为明确未改变。

- [ ] **Step 4: 验证并提交。**

Run: `PYTHONPATH=src uv run python examples/real_llm/selfcheck.py`

Expected: PASS，明确为 Sim/mock 零消耗，不宣称真实代理验收。

```bash
git add examples/real_llm docs/architecture docs/capability-matrix.md
git commit -m "test: add Codex provider capability matrix"
```

### Task 8: 全量与真实验收

**Files:**
- Modify generated by runner: `docs/real-llm-ledger.json`, `docs/real-llm-ledger.md`

- [ ] **Step 1: focused tests。**

Run: `PYTHONPATH=src uv run pytest tests/llm/test_codex_accumulator.py tests/llm/test_codex_wire.py tests/llm/test_codex_responses.py tests/llm/test_audit_redaction_v2.py tests/test_provider_bootstrap.py -v`

Expected: PASS。

- [ ] **Step 2: full tests。**

Run: `PYTHONPATH=src uv run pytest tests/ -v`

Expected: PASS，0 failures。

- [ ] **Step 3: zero-cost selfcheck。**

Run: `PYTHONPATH=src uv run python examples/real_llm/selfcheck.py`

Expected: PASS。

- [ ] **Step 4: 使用已授权 `.env` 运行真实矩阵并生成两份 ledger。**

Run: `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py`

Expected: `provider=codex` 的 Codex 场景 PASS；若凭据/endpoint 不可用则如实记录 FAIL/NOT_EXECUTED，任务不得声称真实验收。

- [ ] **Step 5: 静态质量与最终提交。**

Run: `git diff --check && PYTHONPATH=src uv run ruff check src tests examples`

Expected: exit 0。

```bash
git add docs/real-llm-ledger.json docs/real-llm-ledger.md
git commit -m "docs: record real Codex provider validation"
```

## Plan self-review

- [x] **Spec coverage:** Tasks 1–6 覆盖独立身份、wire、state、done-item 状态机、取消、strict V2 audit、bootstrap 和 sample identity；Tasks 7–8 覆盖真实矩阵、文档、全量和真实 provider 红线。
- [x] **Placeholder scan:** 计划不含 TBD/TODO/“稍后实现”等占位步骤；每个行为都有文件、红测、命令、实现和提交边界。
- [x] **Type consistency:** 全文统一使用 `CodexResponsesClient`、`CodexResponsesAccumulator`、`LlmRequestCommittedV2`、`canonical_attempt_sha256`、`redactions`、`llm_sample_id` 与 dialect `codex-responses-v1`。
