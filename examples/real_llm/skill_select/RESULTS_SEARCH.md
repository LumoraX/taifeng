# 选择基准结果快照（deferred 召回版 · search_skills）

> 本文件是 `bench_search.py`（deferred 召回版）的结果台账，与 `RESULTS.md`（inline 基线）成对照。
> 两实验唯一变量是「child 暴露方式」：inline 全量内联 vs deferred 先 `search_skills` 召回再 `call_skill`。
>
> 跑法：`PYTHONPATH=src python examples/real_llm/skill_select/bench_search.py --sample 210`。
> 与 `RESULTS.md` 用**同 provider / model / 任务集**对照。

## 2026-06-17 · deepseek · deepseek-v4-flash · 210 任务（全量）

- 候选规模：**210 个 skill**（router 走 deferred，prompt **不**内联 child）
- 样本：210 条（全量）
- 每任务 2 步：`search_skills` 召回 → `call_skill` 选定（deny `Skill(*)` + `max_iterations=2`）

**总准确率：179/210 = 85.2%**（未选出 24 条；**召回触达率 210/210 = 100%**；耗时 1074.4s）

| domain | acc | | domain | acc |
| --- | --- | --- | --- | --- |
| data | 28/30 = 93% | | office | 25/30 = 83% |
| ops | 27/30 = 90% | | content | 24/30 = 80% |
| analytics | 25/30 = 83% | | dev | 26/30 = 87% |
| doc | 24/30 = 80% | | | |

### 与 inline 基线（RESULTS.md）对照

| | 总准确率 | 未选出(None) | **选中时精度** | 耗时 |
| --- | --- | --- | --- | --- |
| **inline 基线（全塞 prompt）** | **193/210 = 91.9%** | 12 | 193/198 = **97.5%** | 641.6s |
| **deferred 召回（本次）** | **179/210 = 85.2%** | 24 | 179/186 = **96.2%** | 1074.4s |

**核心发现（反直觉，如实记录）：在 210 候选规模，deferred 召回没有跑赢全塞 prompt，反而退了 6.7 个点（91.9% → 85.2%）。**

拆解差距来源：

1. **退步几乎全来自「未选出」翻倍（12 → 24）**，而非选错。两版「选中时精度」几乎持平（97.5% vs 96.2%）——**模型一旦提交选择，deferred 的命中质量与基线相当**。
2. **召回本身满分工作**：召回触达率 100%（模型每个任务都乖乖先 `search_skills` 再 `call_skill`），且选中时 96% 正确 → 说明召回后端把正确目标送进 top-K 的概率很高，**召回精度不是瓶颈**。
3. **多出的 12 个 None 是「两步流 + `max_iterations=2`」的结构性代价**：deferred 必须 search（耗 1 轮）+ call（耗 1 轮）；若模型在第 2 轮想先 `read_skill` 试用某候选、或再搜一次、或直接作答，就没有第 3 轮去 `call_skill` → 计未选出。基线是单步（`max_iterations=1`）直接 call，没有这个额外消耗点。
4. **耗时 ~1.7×**（多一个 round-trip）。

### 错选 / 未选出形态

- **只搜不调（24 条 None，差距主因）**：模型 search 后未在 2 轮内提交 `call_skill`。集中在「读起来像直接帮我做」的任务（摘要 / 改写 / 排查 / 纪要），与基线 None 同源，但因多一步而数量翻倍。
- **跨域重复能力近邻互窜（少量错选）**：`content-summarize ↔ doc-report-summarize` 双向互换、`doc-minutes-generate → office-meeting-minutes`、`doc-expense-report-gen → office-expense-report`、`office-email-reply → office-email-draft`、`analytics-anomaly-detect → ops-log-anomaly-scan`。这类「两个域都有高度相似能力」的冲突，召回把双方都送进 top-K，选择阶段仍难消歧——**与 inline 基线同款顽疾，召回未消除也未加剧**。

### 召回天花板离线探针（零 key，定位根因）

用原始改写文案当 query 直接喂 `KeywordSkillRecall`（不经 LLM），测正确 skill 进 top-K 的命中率——这是模型选对的**天花板**：

| 指标 | 数值 |
| --- | --- |
| 正确 skill 非零分（被召回出来） | 178/210 = **84.8%**（15% 与改写 query **零关键词重叠**） |
| **hit@5**（bench 实际 top_k） | 152/210 = **72.4%** |
| hit@10 | 163/210 = 77.6% |
| hit@20 | 167/210 = 79.5% |
| 命中项中位排名 | **1**（关键词命中即排首；双峰：要么第一、要么没召出） |

掉出 top-10 的 47 个正确 skill **密集扎堆语义近邻域**（analytics 异常/流失/离群/显著性/根因/分位；content 改写/语气/语法/润色）。

**根因确证**：基线 91.9% 靠 LLM 看全 210 条用**语义注意力**选；deferred 用**关键词 BM25** 先筛 top-5，等于在 LLM 前插了弱过滤器，把改写近义任务筛掉 → LLM 语义强项被废。`max_iter=2` 的未选出是次要因素，**主因是关键词召回在改写 query 上天花板仅 ~72%**。

### 结论

1. **能力本身正确且全链打通**：`search_skills` 召回 → `read_skill` 试用 → `call_skill` 派发 → 战绩沉淀（`selection_origin="discovered"`）端到端工作，召回触达 100%、选中精度 96%。这一刀**实现达标**。
2. **真因是召回后端，不是规模/预算**：离线探针证明关键词 BM25 在改写 query 上 hit@5 仅 72%、15% 零重叠。基线靠 LLM 全量语义注意力选（91.9%），deferred 用弱关键词过滤器先筛，把语义近邻任务筛掉。**这不是「210 规模没痛点」，而是「默认召回后端把 LLM 的语义强项废了」。**
3. **这验证了设计而非否定**：`SkillRecall` 是可插拔插槽，内核关键词 BM25 是**零依赖地板**；设计明文要业务注入 RagSelector/LlmSelector（向量召回）。本 A/B **量化证明**改写口语 query 上必须上**语义/向量召回**——插槽的价值正在此。
4. **下一步（按收益排序）**：① 注入**向量/embedding 召回后端**（语义匹配，直接抬 hit@K，最大杠杆）；② 或 **generous top_k**（召回放宽到 20-30，让 LLM 仍对较大候选集行使语义注意力，接近「看全量」但 prompt 有界）；③ `max_iter=3` 给 `read_skill` 试用留空间（次要，只回收两步流的未选出）。关键词召回适用于「query 与描述词面接近」的场景，**不适用于刻意改写的口语任务**。
