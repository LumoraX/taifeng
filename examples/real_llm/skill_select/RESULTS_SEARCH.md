# 选择基准结果快照（deferred 召回版 · search_skills）

> 本文件是 `bench_search.py`（deferred 召回版）的结果台账，与 `RESULTS.md`（inline 基线）成对照。
> 两实验唯一变量是「child 暴露方式」：inline 全量内联 vs deferred 先 `search_skills` 召回再 `call_skill`。
>
> 跑法：`PYTHONPATH=src python examples/real_llm/skill_select/bench_search.py --sample 210`。
> 与 `RESULTS.md` 用**同 provider / model / 任务集**对照。
>
> **前提澄清（别误读为「内核默认关键词」）**：**内核默认召回是 inline（`skill_recall=None`，LLM 从 prompt 全列 child 里自己找），不是关键词**。本 deferred 基准是为测「关键词召回路径」而**显式注入 `KeywordSkillRecall`** 后端跑的——`KeywordSkillRecall` 是召回阶梯里的**可选后端之一**（默认 inline → 可选关键词 / `LlmSkillRecall` / 业务 RAG），非默认。下文一切「关键词召回 / 召回后端」均指**本基准显式注入的那层**，不代表内核默认。

## 三轮对照总表（同 provider/model/任务集 · deepseek-v4-flash · 210 全量）

| 实验 | router 提示词 | max_iter | 准确率 | 未选出 | 召回触达 | 耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| **基线** inline 全塞 | （直接 call） | 1 | **193/210 = 91.9%** | 12 | — | 641s |
| deferred·首轮 | 口语「一句话概括」query | 2 | 179/210 = 85.2% | 24 | 100% | 1074s |
| **deferred·优化** | **CREATE+ReAct，关键词式 query** | 5 | **190/210 = 90.5%** | **8** | 99.5% | 1397s |

**核心结论（纠正首轮判断）**：提示词优化把 deferred 从 85.2% 拉到 **90.5%，追平基线 91.9%（差 1.4 点在跑测波动内）**，未选出 24→8（比基线还少）。
**主锅是提示词，不是关键词召回后端**——首轮 RESULTS 里「真因是后端」的判断**偏了**：关键词召回在**原始口语 query** 上确实弱（离线探针 hit@5=72%），但**好的 ReAct 提示词让 LLM 把 query 重写成关键词式**，有效召回随之够用 → 追平。即「② 转译」做好了，本基准显式注入的关键词召回（可选后端之一，非内核默认；内核默认是 inline）这块零依赖地板就够撑到此规模。

---

## 详情一：deferred·优化版（CREATE+ReAct router + max_iter=5）

- 提示词按 CREATE（Context/Role/Execute/Action/Target）重写，Action 内嵌 ReAct 循环（思考→search→观察→反思→没中改关键词重搜，最多 3 次）+ Plan-ReAct 计划步（先拆意图成关键词）。
- **总准确率：190/210 = 90.5%**（未选出 8；召回触达 99.5%；耗时 1397.1s）

| domain | acc | | domain | acc |
| --- | --- | --- | --- | --- |
| ops | 30/30 = 100% | | data | 28/30 = 93% |
| dev | 28/30 = 93% | | doc | 28/30 = 93% |
| office | 27/30 = 90% | | analytics | 26/30 = 87% |
| content | 23/30 = 77% | | | |

**剩余 20 错全是真实语义歧义（基线同款顽疾，提示词/关键词均救不了）**：
- 域内近邻：analytics 的 anomaly↔outlier、significance↔ab-test、rootcause↔percentile（统计能力天然相邻）。
- 跨域重复能力：content-summarize↔doc-report-summarize、doc-minutes↔office-meeting-minutes、doc-expense↔office-expense（两域有几乎相同能力）。
- 8 个 None 集中在 content/office 的「直接帮我做」类（模型自答不路由）。
→ 这些需 **skill 描述消歧** 或 **read_skill 试用 / 置信度分流（相位 3）**，非本相位范围。

---

## 详情二：deferred·首轮（口语 query + max_iter=2，已被优化版取代，留作对照）

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

**注意（被优化版实验修正）**：此 72% 是「**用原始口语文案直接当 query**」的天花板——但真实流程里 LLM 会**自己重写 query**。优化版提示词让 LLM 把口语意图拆成关键词式 query（「② 转译」），有效召回随之大幅高于 72%，最终 90.5% ≈ 基线。故此探针测的是**naive query 的地板**，不是召回后端的硬上限。

### 结论（综合三轮 + 探针，已纠偏）

1. **能力正确且全链打通**：`search_skills` 召回 → `read_skill` 试用 → `call_skill` 派发 → 战绩沉淀（`selection_origin="discovered"`）端到端工作。实现达标。
2. **主锅是提示词（②转译），不是关键词后端**：① 口语直喂关键词召回 hit@5 仅 72%（探针）；② 但 ReAct 提示词让 LLM 把 query 重写成关键词式后，deferred 追平基线（90.5% vs 91.9%），未选出还更少（8 vs 12）。**本基准显式注入的关键词召回（可选后端之一，非内核默认——默认是 inline）配好提示词，其零依赖地板足以撑到此规模。**
3. **prompt 与召回后端要匹配**：关键词召回要关键词 query，提示词却引导口语复述 → 自相打架。修提示词（CREATE+ReAct，引导关键词 query + 重搜）是**最便宜的杠杆**，先于换后端。
4. **残差是真实语义歧义**：剩 20 错=域内近邻 + 跨域重复能力 + content 自答 None，**基线同款**，提示词/关键词均不解；需 skill 描述消歧或相位 3 置信度分流/read_skill 试用。
5. **search 的规模价值仍成立**：90.5% ≈ 基线但 **prompt 体积恒定（与 skill 总数无关）**；候选上到千/万级、基线被全塞压垮时，deferred 才显现准确率优势。向量召回（业务注入，ADR 0017③）是更大规模/更高密度近邻时的进一步杠杆，但**非此规模的必需**。
