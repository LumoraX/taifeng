# ADR 0023: skill 发现走 search —— deferred 暴露 + 白名单作用域召回 + confidence 仅透数据

- 状态：Accepted
- 日期：2026-06-17
- 关系：立项依据 ADR 0017 规则②（模型认知回路原语——发现相位）；Relates `docs/superpowers/specs/2026-06-16-skill-capability-acquisition-loop-design.md`（§6 发现相位，本 ADR 是其相位 2 落地）；连回 ADR（无）的 v1 战绩沉淀 [skill-outcome-record](../architecture/capabilities/skill-outcome-record.md)（复用其 `SelectionOrigin`）

## 背景

认知回路设计把「万级 skill 的工具认知回路」拆成发现（⑥）→ 评估 → 派发 → 战绩沉淀（⑦）等相位。v1（战绩沉淀）已落地：每次 `call_skill` 终态记一条 `SkillExecutionRecord`，预留 `selection_origin` / `selection_confidence` 两字段（v1 恒 `whitelist` / `None`），等发现相位填。

相位 2 要解决的真实缺口：caller composite skill 的 `child_skills` 一旦膨胀（几十上百个子专科 / 工具），全部内联进 system prompt 的 `<available_child_skills>` 会**撑爆 context 且稀释注意力**。需要一个「据当前子任务意图先召回 top-K 再决策」的发现机制。

落地前要定四件事的边界，避免做成「带准入的检索引擎」越界：召回作用域多宽、confidence 怎么用、阈值怎么切、溯源怎么连回 v1。

## 决策

### 决策一：召回协议化，内核只定 `SkillRecall` + 零依赖默认实现

`skill/recall.py` 定义 `SkillRecall`（`runtime_checkable` Protocol）+ 数据契约 `SkillCandidate` / `RecallEntry`，内置零依赖 `KeywordSkillRecall`（标准 BM25：idf 加权 + tf 饱和 + 长度饱和归一，零依赖分词）。

> 取舍：曾考虑直接内置向量召回。否决——向量库 / embedding 是**外部成熟服务**（ADR 0017 规则③：内核只定协议、实现走外部）。内核给零依赖关键词默认保证开箱即用，向量 / 外部检索由业务实现同协议替换。协议要求纯函数 / 确定性（禁系统时钟 / 随机源），便于 replay 与测试。

### 决策二：召回作用域 = caller 白名单内 G4 过滤（发现 ≠ 准入）

召回语料池**仅含 caller 的 `child_skills` 经 G4 过滤后的可见集**，且白名单封闭由**内核**钉死（召回后端只能在传入的 `pool` 内排名，无从越权召回池外 skill）。过滤复用 `visibility.visible_child_skills`——它是 `loop/prompt.py` inline 列表那套 G4 过滤的**唯一实现**。

这是本 ADR 最核心的安全边界：**相位 2 只做发现、不碰授权**。

- 召回拿到一个 skill ≠ 有权派发它——准入仍由 `DispatchPolicy`（深度 / 环 / 白名单）在 `call_skill` 时裁决。
- inline 与 deferred **同源同过滤**：否则 deferred 会成为 G4 旁路（LLM 通过召回拿到本应被 `model_invocable=False` 隐藏 / `requires` 不满足的 skill）。同源是治理一致性的结构性保证，不是约定。

> 取舍：曾考虑召回 reachable 全集（跨多层 child）。否决——召回是「为 LLM 选下一个 `call_skill` 目标」服务，目标必须在 caller 直接白名单内；扩到 reachable 全集等于偷偷放宽派发作用域。

### 决策三：超阈值自动 deferred，显式声明优先（单一真相判定）

`visibility.effective_child_recall(entry, child_count, threshold)` 是 system prompt 文本构建与 per-turn 工具裁剪的**唯一判定**：

- `child_recall: inline / deferred` 显式声明优先（强制内联 / 强制召回）。
- `auto`（默认）：G4 过滤后可见 child 数 `> recall_threshold`（构造参数，默认 50）→ deferred，否则 inline。

两侧调同一函数，保证「prompt 是否列 child」与「per-turn 是否暴露 `search_skills`」严格一致（否则会撕裂成「prompt 说没列、工具却不给搜」或反之）。`child_count` 用 G4 过滤后的可见数，与实际会内联 / 召回的池规模一致。

> R2 说明：inline / deferred 决定 entry **静态 system prompt 形状**（pre-turn 决定、整 turn 稳定），不是 mid-turn cache 失效——同一 entry 跨 turn 走同一分支，prefix 稳定。

### 决策四：confidence 仅透数据给 LLM，内核不据其分流

`search_skills` 透给 LLM 的候选含 `confidence`（同次召回内 score 归一到 [0,1]）+ `matched_snippet`，**不外露 score**。confidence 仅供 LLM 二次决策，相位 2 内核任何路径**不据其自动放行 / 拦截 / 降级**。

> 取舍：曾考虑「confidence < 阈值就拦截派发」做自动门控。否决——分流是策略，属后续相位；相位 2 内核只透数据、让 LLM 决策，保持「内核给机制、不替模型做认知判断」的定位。`score` 仅同次召回内可比，跨次无可比性，故不外露、禁持久化做阈值。

### 决策五：选择溯源复用 v1 `discovered` Literal，不新增字段

经 `search_skills` 召回选中再 `call_skill` 派发的 skill，战绩记 `selection_origin="discovered"` + `selection_confidence=<召回 confidence>`；未经召回的派发仍 `whitelist` / `None`。

复用 v1 已预留的 `SelectionOrigin` Literal（`"whitelist"` | `"discovered"`）与 `selection_confidence` 字段，零新增 schema——这正是 v1「长相分预留、发现相位填」的兑现。溯源是 turn 内 best-effort：结构异常时跳过该项、不伪造默认 confidence（非 silent fallback），退化为 v1 行为。

## 影响

- **R1 业务零侵入**：`SkillRecall` 是业务注入缝；`src/` 内无业务概念；`KeywordSkillRecall` 零依赖开箱可用。
- **R2 Cache 友好**：deferred 判定是静态 prompt 形状，非 mid-turn 失效；不返回 `CompressionResult`。
- **R3 可观测**：`skill_search_invoked` / `skill_candidates_returned` 两事件覆盖发现链。
- **R4 可取消**：`SkillRecall.recall` 接收 `CancellationToken`，入口与打分后各 check 一次。
- **R5 可 resume**：召回为读路径不落新持久化状态；溯源结果落进既有 `skill_outcome` JSONL（append-only）。

## 落档

- 能力契约：[capabilities/skill-recall.md](../architecture/capabilities/skill-recall.md)（数据契约 + 行为契约 + 场景）。
- 模块活文档：[skill-system.md](../architecture/skill-system.md) §deferred 暴露与 search_skills 发现流。
- 旋钮：[configurable-knobs.md](../configurable-knobs.md)（`recall_threshold` / `recall_default_top_k` / `recall_max_top_k`）。
