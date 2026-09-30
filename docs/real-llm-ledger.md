# 真实 LLM 验证台账

> **本文件由 `examples/real_llm/capability_matrix.py` 自动生成（数据源 `real-llm-ledger.json`），勿手编辑。**
> 回归红线：基础层（`src/taifeng/{llm,loop,context,conversation}/`）变更必须全量重跑并提交本台账；详见 CLAUDE.md §测试约束。

- **最近一次回归**：2026-09-30 12:21:31 UTC @ `dcf208f`
- **Provider / Model**：codex / gpt-5.6-luna
- **本次跑测场景**：composite_dispatch, read_skill_lazy, orchestration, concurrent_fanout, research_pipeline, product_review, numeric_loop, compression, selective_approval, travel_planner, suspend_resume, turn_rewind, thread_rewind, spawn_join, peer_messaging, wait_any, kernel_knobs, post_turn_review, budget_awareness, token_calibration, skill_inference, tool_output_guard, pinned_periodic, file_search, read_skill_path, compaction_continuity, codex_instructions, codex_image_single, codex_image_order, codex_image_tool_call, codex_tool_image_output, codex_encrypted_state_hot_replay, codex_legacy_jsonl_cold_resume, codex_file_input

## 逐场景结果

| 场景 | 能力 | 结果 | 日期 @ commit | 耗时 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `budget_awareness` | 预算自知提示（穿越 soft_limit 注中性预算事实，ADR 0020） | ✅PASS | 2026-09-30 @ `dcf208f` | 13s |  |
| `codex_encrypted_state_hot_replay` | Codex encrypted state 热重放 | ✅PASS | 2026-09-30 @ `dcf208f` | 10s |  |
| `codex_file_input` | Codex 文件（PDF）输入：随机核对码读出 + 登记 + 脱敏 | ✅PASS | 2026-09-30 @ `dcf208f` | 7s |  |
| `codex_image_order` | Codex 有序多图片语义 | ✅PASS | 2026-09-30 @ `dcf208f` | 4s |  |
| `codex_image_single` | Codex 单图片语义 | ✅PASS | 2026-09-30 @ `dcf208f` | 4s |  |
| `codex_image_tool_call` | Codex 图片驱动 function call | ✅PASS | 2026-09-30 @ `dcf208f` | 4s |  |
| `codex_instructions` | Codex 顶层 instructions | ✅PASS | 2026-09-30 @ `dcf208f` | 4s |  |
| `codex_legacy_jsonl_cold_resume` | Codex 图片/state legacy JSONL 冷恢复 | ✅PASS | 2026-09-30 @ `dcf208f` | 10s |  |
| `codex_tool_image_output` | Codex 工具返回图片（function_call_output 带 input_image） | ✅PASS | 2026-09-30 @ `dcf208f` | 3s |  |
| `compaction_continuity` | 压缩后衔接：保留用户原话 + 续接前言（handoff，ADR 0059） | ✅PASS | 2026-09-30 @ `dcf208f` | 43s |  |
| `composite_dispatch` | composite call_skill 派发 + HITL | ✅PASS | 2026-09-30 @ `dcf208f` | 22s |  |
| `compression` | 上下文压缩（sliding，小窗触发） | ✅PASS | 2026-09-30 @ `dcf208f` | 74s |  |
| `concurrent_fanout` | 并发 fan-out（LLM 自主并行派发） | ✅PASS | 2026-09-30 @ `dcf208f` | 38s |  |
| `file_search` | opt-in glob / grep 只读搜索工具（ADR 0064） | ✅PASS | 2026-09-30 @ `dcf208f` | 6s |  |
| `kernel_knobs` | K2 会话 token 天花板真实触发（resource_limit） | ✅PASS | 2026-09-30 @ `dcf208f` | 3s |  |
| `numeric_loop` | 多轮 run_script 数值调谐（工具循环） | ✅PASS | 2026-09-30 @ `dcf208f` | 37s |  |
| `orchestration` | 声明式编排 parallel/serial/when | ✅PASS | 2026-09-30 @ `dcf208f` | 9s |  |
| `peer_messaging` | 谱系 peer 消息投递（spawn + send_message） | ✅PASS | 2026-09-30 @ `dcf208f` | 11s |  |
| `pinned_periodic` | pinned 任务清单按用户轮数周期重注（ADR 0065） | ✅PASS | 2026-09-30 @ `dcf208f` | 15s |  |
| `post_turn_review` | post_turn 钩子（turn 收尾审计/记忆固化 + 跨 turn 顺序） | ✅PASS | 2026-09-30 @ `dcf208f` | 8s |  |
| `product_review` | fan-out 多 reviewer + 评分聚合 | ✅PASS | 2026-09-30 @ `dcf208f` | 12s |  |
| `read_skill_lazy` | read_skill 懒加载（skill-as-context） | ✅PASS | 2026-09-30 @ `dcf208f` | 16s |  |
| `read_skill_path` | read_skill 读取 skill 目录内附属文件（渐进加载第三层，ADR 0060） | ✅PASS | 2026-09-30 @ `dcf208f` | 7s |  |
| `research_pipeline` | 串行 pipeline（采集→提炼→写作） | ✅PASS | 2026-09-30 @ `dcf208f` | 24s |  |
| `selective_approval` | 差异化授权 + 多路派发 | ✅PASS | 2026-09-30 @ `dcf208f` | 16s |  |
| `skill_inference` | skill 级推理参数（SKILL.md inference 块按 skill 下发，ADR 0056） | ✅PASS | 2026-09-30 @ `dcf208f` | 8s |  |
| `spawn_join` | 分离式并发 spawn + 错峰 HITL + join-barrier 聚合 | ✅PASS | 2026-09-30 @ `dcf208f` | 75s |  |
| `suspend_resume` | HITL 挂起 → Resume 续跑（R5） | ✅PASS | 2026-09-30 @ `dcf208f` | 7s |  |
| `thread_rewind` | thread 寻址 rewind（spawn 子 thread 截断重推） | ✅PASS | 2026-09-30 @ `dcf208f` | 8s |  |
| `token_calibration` | 上下文 token 实测校准（预测下一轮 prompt，ADR 0043） | ✅PASS | 2026-09-30 @ `dcf208f` | 13s |  |
| `tool_output_guard` | PostToolUse 改写工具输出 + 工具结果字节上限（ADR 0061） | ✅PASS | 2026-09-30 @ `dcf208f` | 6s |  |
| `travel_planner` | 三路 fan-out（航班/酒店/活动）+ 综合 | ✅PASS | 2026-09-30 @ `dcf208f` | 18s |  |
| `turn_rewind` | turn 回访重跑（Rewind re_reason） | ✅PASS | 2026-09-30 @ `dcf208f` | 65s |  |
| `wait_any` | any-of-N 等待(wait_any:先到先处理,不等最慢的) | ✅PASS | 2026-09-30 @ `dcf208f` | 11s |  |

## 未执行验证

- **openai_image_input**：`NOT_EXECUTED` — OpenAI API key unavailable in this verification environment; real GPT-5.6 Chat/Responses image matrix was not executed （`PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --provider openai --model gpt-5.6`，2026-08-28 03:18:47 UTC @ `6bd5d62`）

## R3 可观测完整性审计（最近一次全量）

- 发出的事件 kind：31 种
- ✅ 所有发出的事件 kind 都有专用 console 渲染
- ✅ R3 经典事件全部触发

## 判定口径

- **PASS** = 终态完成 ∧ 期望关键事件全命中；**PART** = 完成但缺关键事件；**FAIL** = turn_failed / 未完成 / 场景异常。
- LLM 不配合（不调对应工具）如实记 FAIL/PART，不自动重试美化。
- **stale** = 该场景结果产生于更早的 commit（本次未复跑）。
