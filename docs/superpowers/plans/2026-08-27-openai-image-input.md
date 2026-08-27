# OpenAI 图片输入与双协议 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在保持 OpenAI-compatible 客户端完全兼容的前提下，交付由业务策略显式启用的图片输入，以及独立的 OpenAI Chat 与 Responses 协议客户端。

**Architecture:** conversation 只保存 canonical base64 图片附件；prompt 层重验并投影为 provider-neutral parts 和严格有序 input items。专用 OpenAI adapters 在网络边界分别转换为 Chat 与 Responses；Responses 的 terminal normalized output 是持久化和工具执行唯一事实源，JSONL 原子 batch 保证冷恢复不暴露半个响应。

**Tech Stack:** Python 3.12、Pydantic v2、anyio、httpx/SSE、pytest、SimClient、JSONL Journal。

---

## 文件结构

| 路径 | 职责 |
| --- | --- |
| src/taifeng/conversation/models.py | canonical image attachment、detail、provider state metadata。 |
| src/taifeng/llm/types.py、client.py、events.py、errors.py、image_input.py | parts/items/capabilities、图片校验与稳定错误。 |
| src/taifeng/loop/prompt.py、submission.py、audit_admission.py、pool.py、engine.py、turn.py | admission 注入、历史转换、Responses durable commit、脱敏。 |
| src/taifeng/conversation/store.py、transcript.py、journal | atomic response batch 与 strict-audit round trip。 |
| src/taifeng/context/budget.py、boundaries.py、compaction_view.py | 图像 token/bytes、sample group、密文脱敏压缩视图。 |
| src/taifeng/llm/providers/openai | OpenAI Chat 和 Responses 两套官方协议。 |
| src/taifeng/llm/providers/openai_compat.py | 原行为不变，仅在序列化前拒绝 image/provider state。 |

### Task 1: 图片契约、DTO 与 capabilities

**Files:**
- Create: docs/architecture/capabilities/llm-image-input.md, tests/llm/test_image_input_types.py
- Modify: src/taifeng/conversation/models.py, src/taifeng/llm/types.py, src/taifeng/llm/client.py, src/taifeng/llm/__init__.py

- [ ] **Step 1: 写失败 schema tests。** 覆盖 PNG/JPEG/WebP/GIF MIME union、旧 attachment 缺 detail → auto、纯文本 content 仍为 str、image-only 合法、text/image 都空非法，以及 messages-only/items-only/equivalent/conflicting 四种 ApiRequest 构造路径。

~~~python
def test_request_items_are_canonical_source() -> None:
    request = ApiRequest(model="m", input_items=[
        ApiMessageItem(role="user", content=[TextPart(text="inspect"), IMAGE]),
    ])
    assert request.messages[0].content[1] == IMAGE

def test_conflicting_request_views_fail() -> None:
    with pytest.raises(InvalidRequestError, match="input_items"):
        ApiRequest(model="m", messages=[ApiMessage(role="user", content="a")],
                   input_items=[ApiMessageItem(role="user", content="b")])
~~~

- [ ] **Step 2: 确认红灯。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_image_input_types.py -q

Expected: FAIL，因为 DTO、input item conversion 与 capability helper 尚不存在。

- [ ] **Step 3: 实现严格 DTO。** 定义 frozen ImageAttachmentV1、TextPart、ImagePart、ProviderStateEnvelope、ApiMessageItem、ApiFunctionCallItem、ApiFunctionCallOutputItem、ApiProviderStateItem 和 ModelCapabilities；实现 messages_to_input_items()、input_items_to_messages()，在 ApiRequest validator 强制单一来源。旧 client 通过 helper 自动视为 text-only。

~~~python
@dataclass(frozen=True)
class ModelCapabilities:
    input_modalities: frozenset[Literal["text", "image"]]
    provider: str
    protocol: str
    accepts_provider_state: bool = False
~~~

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_image_input_types.py tests/conversation/test_models_system_injection.py -q

Expected: PASS。

~~~bash
git add docs/architecture/capabilities/llm-image-input.md src/taifeng/conversation/models.py src/taifeng/llm/types.py src/taifeng/llm/client.py src/taifeng/llm/__init__.py tests/llm/test_image_input_types.py
git commit -m "feat: define provider-neutral image input contracts"
~~~

### Task 2: canonical admission、格式检查与成本估算

**Files:**
- Create: src/taifeng/llm/image_input.py, tests/llm/test_image_admission.py
- Modify: src/taifeng/context/budget.py, src/taifeng/llm/errors.py

- [ ] **Step 1: 写失败 admission tests。** 用内嵌最小合法 PNG/JPEG/WebP/单帧 GIF，参数化 non-canonical base64、size/digest 不符、MIME masquerade、动画 GIF、零尺寸、count、单项和总 decoded bytes 边界。断言 disabled policy 在 decode 前拒绝，unknown model 单图 estimate 使用 ceiling 而非零。

~~~python
def test_disabled_policy_rejects_before_decode() -> None:
    with pytest.raises(UnsupportedModalityError):
        admit_image_attachments([MALFORMED_IMAGE], DISABLED_IMAGE_POLICY)

def test_animated_gif_is_not_an_image_input() -> None:
    with pytest.raises(InvalidImageError, match="frame"):
        validate_image_attachment(ANIMATED_GIF, ENABLED_POLICY)
~~~

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_image_admission.py -q

Expected: FAIL，因为 policy、inspector 和错误类型尚不存在。

- [ ] **Step 3: 实现 bounded pure-Python admission。** 定义不可变 ImageInputPolicy、DISABLED_IMAGE_POLICY、InputCostEstimator、ConservativeImageCostEstimator、OpenAIImageCostEstimator。严格按 count → encoded length → strict base64 → decoded size/total → sha256 → MIME signature → dimensions → GIF frame 顺序检查；只用内存且所有 decode 上限受 policy 限制。扩充 estimate_item_tokens/bytes，图片 bytes 包含 canonical base64 与 JSON 开销。

~~~python
def validate_image_attachment(attachment: ImageAttachmentV1,
                              policy: ImageInputPolicy) -> InspectedImage:
    data = decode_canonical_base64(attachment.content, policy.max_item_bytes)
    verify_size_and_digest(data, attachment)
    width, height, frames = inspect_image(data, attachment.media_type)
    if width <= 0 or height <= 0 or frames != 1:
        raise InvalidImageError("invalid image dimensions or frames")
    return InspectedImage(attachment=attachment, width=width, height=height)
~~~

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_image_admission.py tests/context/test_compaction.py -q

Expected: PASS。

~~~bash
git add src/taifeng/llm/image_input.py src/taifeng/llm/errors.py src/taifeng/context/budget.py tests/llm/test_image_admission.py
git commit -m "feat: validate canonical image input"
~~~

### Task 3: 统一 history 转换与 enqueue admission

**Files:**
- Create: tests/loop/test_prompt_image_input.py, tests/loop/test_image_submission_admission.py
- Modify: src/taifeng/loop/prompt.py, submission.py, audit_admission.py, pool.py, engine.py, turn.py

- [ ] **Step 1: 写失败 prompt/submission tests。** 断言 text-only content 和老调用方 bytes 不变；text+multiple 图片为 first text / attachment order；image-only 无空 TextPart；重载 JSONL 也重新校验；disabled/capability/invalid 图片都在 store append 和 actor enqueue 前拒绝，strict audit 只记录 safe submission_rejected。

~~~python
def test_image_only_user_message_has_no_empty_text_part() -> None:
    messages = history_to_api_messages([user_message("", thread_id="t", attachments=[IMAGE])],
        image_input_policy=ENABLED_POLICY, capabilities=IMAGE_CAPS, estimator=ESTIMATOR)
    assert messages[0].content == [ImagePart.from_attachment(IMAGE)]

async def test_invalid_image_is_never_durable(pool, store) -> None:
    with pytest.raises(InvalidImageError):
        await pool.submit_user_message("t", text="x", attachments=[BAD_IMAGE])
    assert await load_all(store, "t") == []
~~~

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/loop/test_prompt_image_input.py tests/loop/test_image_submission_admission.py -q

Expected: FAIL，因为新注入参数和 shared canonicalizer 尚不存在。

- [ ] **Step 3: 实现单一转换。** _convert_history() 产出 ordered ApiInputItem+source index，再调用唯一 input_items_to_messages() 得到 compatibility messages，禁止第二条扫 history 路径。让 prepare_user_message() 同时为 legacy/strict audit 使用；Pool→Engine→TurnRunner 传递 policy/estimator，转换前同时检查 policy 和 client capability；request capture/event 只发 count/bytes/MIME/detail/estimate descriptor。

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/loop/test_prompt_image_input.py tests/loop/test_image_submission_admission.py tests/test_resume_openai_messages.py -q

Expected: PASS。

~~~bash
git add src/taifeng/loop/prompt.py src/taifeng/loop/submission.py src/taifeng/loop/audit_admission.py src/taifeng/loop/pool.py src/taifeng/loop/engine.py src/taifeng/loop/turn.py tests/loop/test_prompt_image_input.py tests/loop/test_image_submission_admission.py tests/test_resume_openai_messages.py
git commit -m "feat: admit images into provider-neutral prompt history"
~~~

### Task 4: Responses metadata、strict audit 与 atomic JSONL batch

**Files:**
- Create: tests/conversation/test_atomic_response_batches.py, tests/loop/test_responses_store_gate.py
- Modify: src/taifeng/conversation/models.py, store.py, transcript.py, journal records/canonical/projector, src/taifeng/loop/audit_llm.py, pool.py

- [ ] **Step 1: 写失败 frame and schema tests。** 注入 begin、每个 item、commit、fsync 前后崩溃；重启只读完整 digest-matching batch。断言 same batch+same digest 是 already committed，same batch+different digest 是 BatchConflictError。验证 provider_state 与三个 metadata key 的 strict journal round trip。

~~~python
async def test_orphan_response_batch_is_invisible_after_restart(tmp_path) -> None:
    await write_lines(tmp_path, [begin_frame([REASONING, ASSISTANT]), item_line(REASONING)])
    assert await load_thread(tmp_path, "t") == []

async def test_batch_id_conflict_is_deterministic(store) -> None:
    await store.append_atomic_batch([ASSISTANT], batch_id="s1")
    with pytest.raises(BatchConflictError):
        await store.append_atomic_batch([OTHER_ASSISTANT], batch_id="s1")
~~~

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/conversation/test_atomic_response_batches.py tests/loop/test_responses_store_gate.py -q

Expected: FAIL，因为 optional atomic store capability 和 frames 尚不存在。

- [ ] **Step 3: 实现协议与 recovery。** 在 store.py 定义 AtomicBatchMessageStore.append_atomic_batch(items, batch_id)、BatchAppendAck 与 conflict error。Jsonl store 在同一 append lock 写 begin/items/commit、flush/fsync；reader 对 old bare lines 保持可见，对 frames 仅发布完整、digest 与 item ids 一致的 first commit。Pool 对 non-audit Responses custom store 做 construct-time gate；strict Journal path 豁免。Journal reason payload white-list provider state，metadata 显式校验 llm_sample_id、provider_output_index、origin_llm_sample_id。

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/conversation/test_atomic_response_batches.py tests/conversation/journal/test_records.py tests/conversation/test_jsonl_writer.py -q

Expected: PASS。

~~~bash
git add src/taifeng/conversation src/taifeng/loop/audit_llm.py src/taifeng/loop/pool.py tests/conversation/test_atomic_response_batches.py tests/loop/test_responses_store_gate.py
git commit -m "feat: persist responses output atomically"
~~~

### Task 5: sample boundary resolver 与安全 compaction view

**Files:**
- Create: src/taifeng/context/boundaries.py, compaction_view.py, tests/context/test_response_sample_boundaries.py
- Modify: src/taifeng/context/compressor.py, strategies/sliding.py, handoff.py, surgical_trim.py

- [ ] **Step 1: 写失败 compaction tests。** 枚举 reasoning→assistant→parallel calls→interleaved outputs、suspension、resume outputs 和 legacy window 的每个 cut；断言 closure 不半删 group、protected tail 相交时收缩、ambiguity fail closed。sentinel encrypted_content 不得出现在 compaction model request、summary、log 或 capture。

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/context/test_response_sample_boundaries.py -q

Expected: FAIL，因为 group resolver 和 redacted view 尚不存在。

- [ ] **Step 3: 实现边界。** 用 llm_sample_id、origin_llm_sample_id、call id 计算连续 closure，legacy 走确定性窗口而任何歧义抛 InvalidHistoryError。所有策略候选 range 先经 resolver，再保护 tail/pinned/suspension。仅 CompactionView.from_items() 可构建 compaction prompt，reasoning 仅留下 visible text/summary，剥除 provider state 和 metadata。

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/context/test_response_sample_boundaries.py tests/context/test_compaction.py tests/loop/test_compaction_hardening.py -q

Expected: PASS。

~~~bash
git add src/taifeng/context tests/context/test_response_sample_boundaries.py
git commit -m "feat: preserve response sample boundaries during compaction"
~~~

### Task 6: OpenAI shared transport 与专用 Chat 客户端

**Files:**
- Create: src/taifeng/llm/providers/openai/__init__.py, _shared.py, chat.py, tests/llm/test_openai_chat.py
- Modify: src/taifeng/llm/providers/__init__.py, openai_compat.py

- [ ] **Step 1: 写 Chat wire/SSE red tests。** mock transport 断言 store=false、image wire 是 image_url.url=data:mime;base64，保留 text、tool schema/results、structured output、usage/rate-limit/request-id/cancel。断言 GPT-5.6 Chat+tools+non-none effort 网络前报 unsupported_combination；compat image 或 provider state 网络前 fail-closed，纯文本 old payload bit-for-bit 不变。

~~~python
def test_chat_uses_data_url_and_store_false(chat_session) -> None:
    payload = chat_session._build_payload(IMAGE_REQUEST)
    assert payload["store"] is False
    assert payload["messages"][-1]["content"][1]["type"] == "image_url"
    assert payload["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
~~~

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_openai_chat.py tests/llm/test_openai_compat.py -q

Expected: FAIL，因为专用 adapter 不存在。

- [ ] **Step 3: 实现。** _shared.py 仅承载官方 auth/base URL/SSE/最终 JSON bytes guard，复用 generic error/usage classifiers；chat.py 声明 text+image/chat capabilities，并从 canonical input_items derived messages 序列化；openai_compat.py 仅加入 assert_text_only_request()，不加 store/new official fields。

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_openai_chat.py tests/llm/test_openai_compat.py tests/llm/test_sse_parse_shared.py -q

Expected: PASS。

~~~bash
git add src/taifeng/llm/providers/__init__.py src/taifeng/llm/providers/openai src/taifeng/llm/providers/openai_compat.py tests/llm/test_openai_chat.py tests/llm/test_openai_compat.py
git commit -m "feat: add OpenAI Chat image client"
~~~

### Task 7: OpenAI Responses mapper、accumulator 与 terminal commit

**Files:**
- Create: src/taifeng/llm/providers/openai/responses.py, tests/llm/test_openai_responses.py
- Modify: src/taifeng/llm/events.py, errors.py, src/taifeng/loop/turn.py

- [ ] **Step 1: 写 Responses wire/event red tests。** 断言 store=false、include reasoning.encrypted_content、没有 previous_response_id；items 用 input_text/input_image，tools 为 flat function，structured output 在 text.format。写 interleaved reasoning/message/parallel function calls events，assert one normalized event before completed, unique/increasing index, one tool done per call id。测试 refusal、failed/incomplete/cancel、missing/duplicate terminal items 和 delta-vs-terminal mismatch 都不提交。

~~~python
async def test_normalized_output_precedes_completed(session) -> None:
    events = [event async for event in session.stream(RESPONSE_REQUEST)]
    assert [event.kind for event in events][-2:] == ["normalized_output", "completed"]

def test_foreign_provider_state_fails_before_network(responses_session) -> None:
    with pytest.raises(InvalidHistoryError):
        responses_session._build_payload(FOREIGN_STATE_REQUEST)
~~~

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_openai_responses.py -q

Expected: FAIL，因为 adapter、internal event 和 accumulator 不存在。

- [ ] **Step 3: 实现 Responses client。** ResponsesAttemptAccumulator 使用 frozen discriminated normalized reasoning/message/function/refusal items，并在 response.completed 以 whitelisted projection finalize。preview deltas 只 emit UI events；terminal 对已见 text/arguments 做 byte comparison；refusal 映射 content-filter。完成时按顺序 emit 唯一 internal normalized_output，随后 usage/cache 和 completed。

- [ ] **Step 4: 改 TurnRunner durable path。** 对 Responses client 要求每 attempt 恰有一个 normalized event；按 output index 生成 reasoning/assistant/function items、sample metadata 与 provider state，strict audit 或 append_atomic_batch(batch_id=llm_sample_id) ack 后再推进 history 和 dispatch tools。tool output 携带 matching origin_llm_sample_id；error/cancel/retry 销毁 accumulator。

- [ ] **Step 5: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_openai_responses.py tests/loop/test_reasoning_passback.py tests/loop/test_cancellation.py -q

Expected: PASS。

~~~bash
git add src/taifeng/llm/events.py src/taifeng/llm/errors.py src/taifeng/llm/providers/openai/responses.py src/taifeng/loop/turn.py tests/llm/test_openai_responses.py
git commit -m "feat: add OpenAI Responses image client"
~~~

### Task 8: Sim、cold resume、exports 与端到端验证

**Files:**
- Create: tests/llm/test_sim_image_input.py, tests/loop/test_openai_image_cold_resume.py
- Modify: src/taifeng/llm/providers/sim/client.py, contract.py, shape.py, src/taifeng/__init__.py, llm/__init__.py

- [ ] **Step 1: 写 Sim/E2E red tests。** Sim 仅检查 part count/MIME/detail/digest/order、不声称视觉理解。用 mock Chat/Responses 走 user item → JSONL → prompt → payload → normalized output → tool history，关闭再建 Engine 后断言 output payload 完全相等；有 foreign Responses state 的 Chat resume 必须 invalid_history。

- [ ] **Step 2: 运行红灯。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_sim_image_input.py tests/loop/test_openai_image_cold_resume.py -q

Expected: FAIL，因为 Sim shape 未识别 parts 与 exports 未完成。

- [ ] **Step 3: 实现最小 Sim extension。** 仅新增 redacted image_inputs descriptor；公开导出 ImageAttachmentV1、ImageInputPolicy、TextPart、ImagePart、OpenAIChatClient、OpenAIResponsesClient，不改变 OpenAICompatClient 导入路径、构造参数、Chat SSE 或 DeepSeek 子类关系。

- [ ] **Step 4: 验证并提交。**

Run: PYTHONPATH=src uv run pytest tests/llm/test_sim_image_input.py tests/loop/test_openai_image_cold_resume.py tests/llm/test_sim_contract.py -q

Expected: PASS。

~~~bash
git add src/taifeng/__init__.py src/taifeng/llm tests/llm/test_sim_image_input.py tests/loop/test_openai_image_cold_resume.py
git commit -m "test: cover image input conformance and recovery"
~~~

### Task 9: 文档、真实 GPT-5.6 fixture、台账和全量证据

**Files:**
- Create: examples/real_llm/fixtures/inventory-label.png, examples/real_llm/test_openai_image_matrix.py
- Modify: docs architecture live pages, docs/capability-matrix.md, docs/configurable-knobs.md, docs/real-llm-ledger.json, docs/real-llm-ledger.md, examples/real_llm/selfcheck.py, capability_matrix.py

- [ ] **Step 1: 添加可审核 fixture 和 real scenarios。** fixture 只含良性库存序列号与几何形状（不是 CAPTCHA）。matrix 检查 Chat/Responses 单图、多图顺序、图片驱动 tool call；Responses 还检查 store=false encrypted-state replay、工具结果下一轮与 cold resume。所有 assertions 检查 serial/geometry、protocol tag、usage/tool events 和 telemetry/capture 无正文。

- [ ] **Step 2: 同步活文档。** 更新 capability contract/readme、LLM client、agent loop、context compression、conversation、overview、knobs、matrix；写清 explicit DI、disabled default、canonical persistence、atomic recovery、compaction redaction、compat boundary 和真实验证列。

- [ ] **Step 3: 零消耗 selfcheck。**

Run: PYTHONPATH=src uv run python examples/real_llm/selfcheck.py

Expected: PASS。

- [ ] **Step 4: 跑全量自动化。**

Run: PYTHONPATH=src uv run pytest tests/ -v

Expected: PASS。

- [ ] **Step 5: 有真实凭据时运行 GPT-5.6 矩阵。**

Run: PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --provider openai --model gpt-5.6

Expected: Chat/Responses 的 serial/geometry semantic assertions PASS；没有凭据或 endpoint 时只报告“未执行”，不把代码/单测等同真实验收。

- [ ] **Step 6: 更新真实台账、最终提交。**

~~~bash
PYTHONPATH=src uv run pytest tests/ -v
git add docs examples/real_llm
git commit -m "docs: document OpenAI image input capability"
git status --short
~~~

Expected: full pytest PASS，台账有真实执行结果或明确未执行状态。

## Plan self-review

- [ ] **Spec coverage:** Tasks 1–3 覆盖 canonical attachment、policy/capability/estimator 和转换/提交；Tasks 4–5 覆盖 strict audit、atomic recovery、sample closure、密文脱敏；Tasks 6–7 覆盖两种 OpenAI wire 协议、compat 保持、normalized output；Tasks 8–9 覆盖 Sim/cold resume/public API/documentation/real evidence。
- [ ] **Placeholder scan:** rg -n -i 'TODO|TBD|implement later|add appropriate|similar to task' docs/superpowers/plans/2026-08-27-openai-image-input.md 无匹配。
- [ ] **Type consistency:** 所有任务统一使用 ImageInputPolicy、InputCostEstimator、ApiInputItem、ProviderStateEnvelope、llm_sample_id、origin_llm_sample_id、normalized_output 和 append_atomic_batch。
