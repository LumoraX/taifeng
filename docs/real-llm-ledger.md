# 真实 LLM 验证台账

> **本文件由 `examples/real_llm/capability_matrix.py` 自动生成（数据源 `real-llm-ledger.json`），勿手编辑。**
> 回归红线：基础层（`src/taifeng/{llm,loop,context,conversation}/`）变更必须全量重跑并提交本台账；详见 CLAUDE.md §测试约束。

- **最近一次回归**：2026-09-02 03:22:20 UTC @ `d80f712`
- **Provider / Model**：codex / gpt-5.6-luna
- **本次跑测场景**：composite_dispatch, read_skill_lazy, orchestration, concurrent_fanout, research_pipeline, product_review, numeric_loop, compression, selective_approval, travel_planner, suspend_resume, turn_rewind, thread_rewind, spawn_join, peer_messaging, kernel_knobs, post_turn_review, budget_awareness, codex_instructions, codex_image_single, codex_image_order, codex_image_tool_call, codex_encrypted_state_hot_replay, codex_legacy_jsonl_cold_resume

## 逐场景结果

| 场景 | 能力 | 结果 | 日期 @ commit | 耗时 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `budget_awareness` | 预算自知提示（穿越 soft_limit 注中性预算事实，ADR 0020） | ✅PASS | 2026-09-02 @ `d80f712` | 9s |  |
| `codex_encrypted_state_hot_replay` | Codex encrypted state 热重放 | ✅PASS | 2026-09-02 @ `d80f712` | 10s |  |
| `codex_image_order` | Codex 有序多图片语义 | ✅PASS | 2026-09-02 @ `d80f712` | 3s |  |
| `codex_image_single` | Codex 单图片语义 | ✅PASS | 2026-09-02 @ `d80f712` | 3s |  |
| `codex_image_tool_call` | Codex 图片驱动 function call | ✅PASS | 2026-09-02 @ `d80f712` | 2s |  |
| `codex_instructions` | Codex 顶层 instructions | ✅PASS | 2026-09-02 @ `d80f712` | 2s |  |
| `codex_legacy_jsonl_cold_resume` | Codex 图片/state legacy JSONL 冷恢复 | ✅PASS | 2026-09-02 @ `d80f712` | 10s |  |
| `composite_dispatch` | composite call_skill 派发 + HITL | ✅PASS | 2026-09-02 @ `d80f712` | 40s |  |
| `compression` | 上下文压缩（sliding，小窗触发） | ✅PASS | 2026-09-02 @ `d80f712` | 74s |  |
| `concurrent_fanout` | 并发 fan-out（LLM 自主并行派发） | ✅PASS | 2026-09-02 @ `d80f712` | 106s |  |
| `kernel_knobs` | K2 会话 token 天花板真实触发（resource_limit） | ✅PASS | 2026-09-02 @ `d80f712` | 2s |  |
| `numeric_loop` | 多轮 run_script 数值调谐（工具循环） | ✅PASS | 2026-09-02 @ `d80f712` | 89s |  |
| `orchestration` | 声明式编排 parallel/serial/when | ✅PASS | 2026-09-02 @ `d80f712` | 34s |  |
| `peer_messaging` | 谱系 peer 消息投递（spawn + send_message） | ✅PASS | 2026-09-02 @ `d80f712` | 12s |  |
| `post_turn_review` | post_turn 钩子（turn 收尾审计/记忆固化 + 跨 turn 顺序） | ✅PASS | 2026-09-02 @ `d80f712` | 19s |  |
| `product_review` | fan-out 多 reviewer + 评分聚合 | ✅PASS | 2026-09-02 @ `d80f712` | 14s |  |
| `read_skill_lazy` | read_skill 懒加载（skill-as-context） | ✅PASS | 2026-09-02 @ `d80f712` | 46s |  |
| `research_pipeline` | 串行 pipeline（采集→提炼→写作） | ✅PASS | 2026-09-02 @ `d80f712` | 45s |  |
| `selective_approval` | 差异化授权 + 多路派发 | ✅PASS | 2026-09-02 @ `d80f712` | 19s |  |
| `spawn_join` | 分离式并发 spawn + 错峰 HITL + join-barrier 聚合 | ✅PASS | 2026-09-02 @ `d80f712` | 42s |  |
| `suspend_resume` | HITL 挂起 → Resume 续跑（R5） | ✅PASS | 2026-09-02 @ `d80f712` | 8s |  |
| `thread_rewind` | thread 寻址 rewind（spawn 子 thread 截断重推） | ✅PASS | 2026-09-02 @ `d80f712` | 7s |  |
| `travel_planner` | 三路 fan-out（航班/酒店/活动）+ 综合 | ✅PASS | 2026-09-02 @ `d80f712` | 22s |  |
| `turn_rewind` | turn 回访重跑（Rewind re_reason） | ✅PASS | 2026-09-02 @ `d80f712` | 81s |  |

## 未执行验证

- **openai_image_input**：`NOT_EXECUTED` — OpenAI API key unavailable in this verification environment; real GPT-5.6 Chat/Responses image matrix was not executed （`PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --provider openai --model gpt-5.6`，2026-08-28 03:18:47 UTC @ `6bd5d62`）

## R3 可观测完整性审计（最近一次全量）

- 发出的事件 kind：25 种
- ✅ 所有发出的事件 kind 都有专用 console 渲染
- ✅ R3 经典事件全部触发

## 判定口径

- **PASS** = 终态完成 ∧ 期望关键事件全命中；**PART** = 完成但缺关键事件；**FAIL** = turn_failed / 未完成 / 场景异常。
- LLM 不配合（不调对应工具）如实记 FAIL/PART，不自动重试美化。
- **stale** = 该场景结果产生于更早的 commit（本次未复跑）。
