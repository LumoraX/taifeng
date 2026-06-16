# 选择基准结果快照

> 真实 LLM 跑测记录。换 provider/model 或改 skill 集后重跑会变化；此为一次基线参考。

## 2026-06-16 · deepseek-v4-flash · 全量 210 任务

- 候选规模：**210 个 skill**（router 一次性暴露全部 child）
- 样本：全部 210 条任务（每个 skill 各测一次）
- 每任务 1 次 LLM 调用（deny `Skill(*)` + `max_iterations=1`，叶子不执行）

**总准确率：193/210 = 91.9%**（未选出 12 条；耗时 641.6s）

| domain | acc | | domain | acc |
| --- | --- | --- | --- | --- |
| data | 30/30 = 100% | | office | 28/30 = 93% |
| ops | 29/30 = 97% | | content | 26/30 = 87% |
| analytics | 28/30 = 93% | | dev | 26/30 = 87% |
| doc | 26/30 = 87% | | | |

### 17 条错选的形态（这才是重点）

1. **语义近邻误选**（候选越相似越难分）：
   - `analytics-outlier-explain` → `analytics-rootcause-drilldown`（都是"找原因"）
   - `analytics-percentile-benchmark` → `analytics-distribution-compare`
   - `office-timezone-convert` → `office-schedule-meeting`
   - `dev-bug-localize` → `ops-latency-breakdown`（"网络超时"被当成 ops 延迟问题，跨域误选）
2. **跨域重复能力**（真冲突，需治理）：
   - `doc-minutes-generate` → `office-meeting-minutes`（两个域都有"会议纪要"能力）
3. **未选出（12 条）**：任务读起来像"直接帮我做这件事"（摘要/风险标记/堆栈分析/依赖升级），
   模型倾向自己作答而非路由调用。`max_iterations=1` 下没等到 call_skill 即终态 → 计未选出。
   故 91.9% 是**下界**；放宽迭代或加强 router 指令可能更高。

### 结论

当前机制（把 210 个候选全量塞进 prompt 让 LLM 硬选）在该模型上 ≈92% 选对，错的几乎全集中在
**语义近邻 / 跨域重复**之间。这正是「认知回路 phase 2：语义检索 + 渐进式披露」（`search_skills`
先按语义召回 top-K，再让模型在少数候选里选）要解决的问题——把"在 200+ 个里硬选"降为"在
少数候选里选"，并暴露跨域重复能力供治理。
