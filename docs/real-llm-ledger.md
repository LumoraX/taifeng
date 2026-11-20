# 真实 LLM 验证台账

> **本文件由 `examples/real_llm/capability_matrix.py` 自动生成（数据源 `real-llm-ledger.json`），勿手编辑。**
> 回归红线：基础层（`src/taifeng/{llm,loop,context,conversation}/`）变更必须全量重跑并提交本台账；详见 CLAUDE.md §测试约束。

- **最近一次回归**：2026-11-20 04:43:38 UTC @ `0454df5`
- **Provider / Model**：openai / gpt-6.1-sol
- **本次跑测场景**：execution_observability, offload_workspace, composite_dispatch, read_skill_lazy, orchestration, concurrent_fanout, research_pipeline, product_review, numeric_loop, compression, selective_approval, travel_planner, suspend_resume, turn_rewind, thread_rewind, spawn_join, peer_messaging, wait_any, kernel_knobs, post_turn_review, budget_awareness, token_calibration, skill_inference, tool_output_guard, pinned_periodic, file_search, read_skill_path, compaction_continuity, plugin_tool, openai_chat_image_single, openai_chat_image_order, openai_responses_image_single, openai_responses_image_order, openai_chat_image_tool, openai_responses_image_tool_replay, openai_responses_image_cold_resume, openai_responses_image_restore, openai_responses_image_ref_restore

## 逐场景结果

| 场景 | 能力 | 结果 | 日期 @ commit | 耗时 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `budget_awareness` | 预算自知提示（穿越 soft_limit 注中性预算事实，ADR 0020） | ✅PASS | 2026-11-20 @ `0454df5` | 19s |  |
| `codex_encrypted_state_hot_replay` | Codex encrypted state 热重放 | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 13s |  |
| `codex_file_input` | Codex 文件（PDF）输入：随机核对码读出 + 登记 + 脱敏 | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 6s |  |
| `codex_image_order` | Codex 有序多图片语义 | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 3s |  |
| `codex_image_single` | Codex 单图片语义 | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 4s |  |
| `codex_image_tool_call` | Codex 图片驱动 function call | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 3s |  |
| `codex_instructions` | Codex 顶层 instructions | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 3s |  |
| `codex_legacy_jsonl_cold_resume` | Codex 图片/state legacy JSONL 冷恢复 | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 13s |  |
| `codex_tool_image_output` | Codex 工具返回图片（function_call_output 带 input_image） | ✅PASS | 2026-11-15 @ `aa5bd48`（stale） | 4s |  |
| `compaction_continuity` | 压缩后衔接：保留用户原话 + 续接前言（handoff，ADR 0059） | ✅PASS | 2026-11-20 @ `0454df5` | 51s |  |
| `composite_dispatch` | composite call_skill 派发 + HITL | ✅PASS | 2026-11-20 @ `0454df5` | 28s |  |
| `compression` | 上下文压缩（sliding，小窗触发） | ✅PASS | 2026-11-20 @ `0454df5` | 160s |  |
| `concurrent_fanout` | 并发 fan-out（LLM 自主并行派发） | ✅PASS | 2026-11-20 @ `0454df5` | 94s |  |
| `execution_observability` | 同 skill 并行执行身份、上下文与自身用量取证 | ✅PASS | 2026-11-20 @ `0454df5` | 19s |  |
| `file_search` | opt-in glob / grep 只读搜索工具（ADR 0064） | ✅PASS | 2026-11-20 @ `0454df5` | 7s |  |
| `kernel_knobs` | K2 会话 token 天花板真实触发（resource_limit） | ✅PASS | 2026-11-20 @ `0454df5` | 3s |  |
| `numeric_loop` | 多轮 run_script 数值调谐（工具循环） | ✅PASS | 2026-11-20 @ `0454df5` | 47s |  |
| `offload_workspace` | offload 与 file_read 共享非本机工作区，stub 后分页回读 | ✅PASS | 2026-11-20 @ `0454df5` | 10s |  |
| `openai_chat_image_order` | OpenAI Chat 多图顺序 | ❌CODE_FAILURE | 2026-11-20 @ `0454df5` | 3s | ServerError: {"error":{"message":"codex channel: /v1/chat/completions endpoint not supported (request id: 202611200442402259520758268d9d65vIYMD6E)","type":"new_api_error","param":"","code":"convert_request_failed"}} |
| `openai_chat_image_single` | OpenAI Chat 单图库存语义 | ❌CODE_FAILURE | 2026-11-20 @ `0454df5` | 1s | ServerError: {"error":{"message":"codex channel: /v1/chat/completions endpoint not supported (request id: 202611200442377483549548268d9d6IALil4qn)","type":"new_api_error","param":"","code":"convert_request_failed"}} |
| `openai_chat_image_tool` | OpenAI Chat 图片驱动工具调用 | ⚠️TRANSIENT_FAILURE | 2026-11-20 @ `0454df5` | 1s | ServerError: {"error":{"message":"codex channel: /v1/chat/completions endpoint not supported (request id: 202611200442545045285818268d9d6qZRHrWVq)","type":"new_api_error","param":"","code":"convert_request_failed"}} |
| `openai_responses_image_cold_resume` | Responses 图片与状态 JSONL 冷恢复 | ✅PASS | 2026-11-20 @ `0454df5` | 15s |  |
| `openai_responses_image_order` | OpenAI Responses 多图顺序 | ✅PASS | 2026-11-20 @ `0454df5` | 9s |  |
| `openai_responses_image_ref_restore` | 图片字节在附件存储、会话只留引用；驱逐后模型经 attachment:<ref> 读回原图（ADR 0148） | ✅PASS | 2026-11-20 @ `0454df5` | 14s |  |
| `openai_responses_image_restore` | 被驱逐的图片落盘留路径，模型用 file_read 读回原图（ADR 0147） | ✅PASS | 2026-11-20 @ `0454df5` | 15s |  |
| `openai_responses_image_single` | OpenAI Responses 单图库存语义 | ✅PASS | 2026-11-20 @ `0454df5` | 4s |  |
| `openai_responses_image_tool_replay` | Responses 图片工具、结果下一轮与 encrypted state | ✅PASS | 2026-11-20 @ `0454df5` | 15s |  |
| `orchestration` | 声明式编排 parallel/serial/when | ✅PASS | 2026-11-20 @ `0454df5` | 16s |  |
| `peer_messaging` | 谱系 peer 消息投递（spawn + send_message） | ✅PASS | 2026-11-20 @ `0454df5` | 20s |  |
| `pinned_periodic` | pinned 任务清单按用户轮数周期重注（ADR 0065） | ✅PASS | 2026-11-20 @ `0454df5` | 24s |  |
| `plugin_tool` | 插件注册的工具被真实模型调用 + 插件生命周期事件（ADR 0144） | ✅PASS | 2026-11-20 @ `0454df5` | 6s |  |
| `post_turn_review` | post_turn 钩子（turn 收尾审计/记忆固化 + 跨 turn 顺序） | ✅PASS | 2026-11-20 @ `0454df5` | 23s |  |
| `product_review` | fan-out 多 reviewer + 评分聚合 | ✅PASS | 2026-11-20 @ `0454df5` | 19s |  |
| `read_skill_lazy` | read_skill 懒加载（skill-as-context） | ✅PASS | 2026-11-20 @ `0454df5` | 34s |  |
| `read_skill_path` | read_skill 读取 skill 目录内附属文件（渐进加载第三层，ADR 0060） | ✅PASS | 2026-11-20 @ `0454df5` | 10s |  |
| `research_pipeline` | 串行 pipeline（采集→提炼→写作） | ✅PASS | 2026-11-20 @ `0454df5` | 25s |  |
| `selective_approval` | 差异化授权 + 多路派发 | ✅PASS | 2026-11-20 @ `0454df5` | 20s |  |
| `skill_inference` | skill 级推理参数（SKILL.md inference 块按 skill 下发，ADR 0056） | ✅PASS | 2026-11-20 @ `0454df5` | 12s |  |
| `spawn_join` | 分离式并发 spawn + 错峰 HITL + join-barrier 聚合 | ✅PASS | 2026-11-20 @ `0454df5` | 163s |  |
| `suspend_resume` | HITL 挂起 → Resume 续跑（R5） | ✅PASS | 2026-11-20 @ `0454df5` | 10s |  |
| `thread_rewind` | thread 寻址 rewind（spawn 子 thread 截断重推） | ✅PASS | 2026-11-20 @ `0454df5` | 12s |  |
| `token_calibration` | 上下文 token 实测校准（预测下一轮 prompt，ADR 0043） | ✅PASS | 2026-11-20 @ `0454df5` | 10s |  |
| `tool_output_guard` | PostToolUse 改写工具输出 + 工具结果字节上限（ADR 0061） | ✅PASS | 2026-11-20 @ `0454df5` | 7s |  |
| `travel_planner` | 三路 fan-out（航班/酒店/活动）+ 综合 | ✅PASS | 2026-11-20 @ `0454df5` | 23s |  |
| `turn_rewind` | turn 回访重跑（Rewind re_reason） | ✅PASS | 2026-11-20 @ `0454df5` | 139s |  |
| `wait_any` | any-of-N 等待(wait_any:先到先处理,不等最慢的) | ✅PASS | 2026-11-20 @ `0454df5` | 15s |  |

## R3 可观测完整性审计（最近一次全量）

- 发出的事件 kind：35 种
- ✅ 所有发出的事件 kind 都有专用 console 渲染
- ✅ R3 经典事件全部触发

## 判定口径

- **PASS** = 终态完成 ∧ 期望关键事件全命中；**PARTIAL** = 完成但缺关键事件；**TRANSIENT_FAILURE** = provider / 网络 / 服务临时故障；**CODE_FAILURE** = 场景或内核失败。
- LLM 不配合（不调对应工具）如实记 FAIL/PART，不自动重试美化。
- **stale** = 该场景结果产生于更早的 commit（本次未复跑）。
