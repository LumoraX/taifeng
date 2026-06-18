# ADR 0024: skill 召回自动发现走 opt-in 总闸 + 召回后接验证门（输入要求适配精验）

- 状态：Accepted
- 日期：2026-06-18
- 关系：Amends #0023（订正其「内核默认召回后端」表述、把「超量自动召回」从默认改为 opt-in 总闸）；立项依据 ADR 0017 规则②（模型认知回路原语——发现 / 评估相位）；连回 [skill-outcome-record](../architecture/capabilities/skill-outcome-record.md)（溯源沿用其 `SelectionOrigin`）；Relates `docs/superpowers/specs/2026-06-18-skill-recall-verify-pipeline-design.md`

> **对 0023 的修订（Amends #0023）**：ADR 0023 初稿一度把「内核默认召回后端」误记为 `KeywordSkillRecall`（默认关键词召回），其末尾「2026-06-18 订正」已就地纠正为 **inline（`skill_recall=None`，LLM 从上下文全列自己选，零额外 LLM 调用）**。本 ADR 进一步在该订正之上做**两件新决策**：① 把「超量子 skill 时自动启用 LLM 召回 / 验证」做成**显式 opt-in 总闸**（`enable_auto_discovery`），而**不是**默认行为——保留 `None=inline` 零成本默认语义不变；② 在召回之后新增一道**验证门**（`SkillVerifier`），据完整 SKILL.md body 判输入要求适配，滤掉「描述像但实际用不上」的误召。

## 背景

ADR 0023 落地了召回相位（相位 2）：`SkillRecall` 协议 + `KeywordSkillRecall` / `LlmSkillRecall` 可选注入后端 + deferred 暴露判定，默认 `skill_recall=None` 走 inline。但它留下两个真实缺口：

1. **开箱即用缺口**：要让「超量子 skill 自动走 LLM 召回」生效，业务必须手动 `LlmSkillRecall(model_client)` 注入——构造侧已有 `model_client`，这步纯样板。需要一个总闸让自动发现开箱即用，又不改默认零成本语义。
2. **召回只看长相**：召回只据 `description`（长相，浅）粗筛，很多误召是「描述像但实际要的输入给不了」（如某 skill 描述贴切但正文声明要一份本任务拿不到的结构化输入）。召回准不代表适配。需要一道「拉完整 body 判输入要求是否满足」的精验，把误召滤掉再交主 LLM 选。

落地前要定四件事的边界：自动发现该默认开还是 opt-in、验证判什么（适配 vs 能否跑通）、长相分与适配分如何不混用、空召回 / 全不适用如何不 silent。

## 决策

### 决策一：自动发现走 opt-in 总闸 `enable_auto_discovery`，不改 `None=inline` 默认

新增构造参数 `enable_auto_discovery: bool = False`（`EnginePool.create` / `EnginePool.__init__`）：

- **关（默认 False）**：维持 0023 现状——`skill_recall=None` = inline（LLM 从 prompt 全列 child 自己选，零额外 LLM 调用）、`skill_verifier=None` = 不验证。**默认零额外成本，最小惊讶。**
- **开（True）**：在「未显式注入」处自动兜底——`skill_recall=None` 自动用 `LlmSkillRecall(model_client)`、`skill_verifier=None` 自动用 `LlmSkillVerifier(model_client)`；deferred 判定照旧按 `recall_threshold` 伸缩（child > 阈值 → deferred）。
- **显式注入优先于总闸**：业务显式传 `KeywordSkillRecall` / 业务 RAG / `LlmSkillRecall` → 直接启用（**不依赖总闸**，也不被总闸覆盖）；显式传 `skill_verifier` → 直接启用验证（与 recall 正交，可「keyword 召回 + LLM 验证」）。
- **禁 silent**：显式声明 `child_recall: deferred` 却**既无注入又没开总闸** → 抛 `SkillValidationError`（启动期 fail-fast，不静默降级 inline）。

> 取舍：曾考虑「超量子 skill 时默认自动开 LLM 召回 + 验证」。否决——这会让默认路径偷偷烧 LLM（违背最小惊讶），且把 0023 刚纠正过来的「默认 inline 零成本」语义又改回去。做成 opt-in 总闸：默认面（零成本 inline）与回炉面都最小，业务一行 `enable_auto_discovery=True` 即获完整自动发现。

### 决策二：召回之后新增验证门（SkillVerifier）——判输入要求适配，不判能否跑通

`skill/verify.py` 新增 `SkillVerifier`（`runtime_checkable` Protocol）+ 数据契约 `VerifiedCandidate` + 默认实现 `LlmSkillVerifier` + `SkillVerifyParseError`。这是认知回路⑤「试用」的精验门：

- **召回看长相、验证看 body**：召回只据 `description`（浅）；验证拉**完整 SKILL.md body**，LLM 判「就当前任务能提供的输入 / 条件，该能力正文里**声明要的输入是否满足、前提是否具备**」。
- **判适配，不判能否跑通**：只判「输入要求 / 前提是否适配」，**不**判该 skill 能否成功跑通、也不判它好不好——能否跑通要等真正派发，是后续相位的事。
- **长相与适配分字段**（延续 0023 防呆）：`VerifiedCandidate` 把 `recall_confidence`（召回长相）与 `verify_confidence`（验证适配）**分开存**，语义不同、不可混用——不拿长相去喂提拔，也不把适配分误当长相。

> 取舍：曾考虑召回直接拉 body 一步到位判适配。否决——召回要在整个 pool 上排名（量大），拉全部 body 会撑爆 prompt；分成「召回宽筛（看 desc）→ 验证精验前 N 个（看 body）」两段，既控成本又分离长相 / 适配两种判断。

### 决策三：search_skills 置信路由禁 silent，全不适用走显式 no_match

验证启用时，`search_skills` 流程为「召回 → 验证 → 置信路由」：

- 有 applicable 候选 → 返回 `[{skill_id, description, confidence, reason}]`（`confidence` 键名**沿用**，值 = `verify_confidence`，保 turn 溯源自然读取不打断）。
- 召回了但全不适用、或召回本就空 → 返回**显式** `{"no_match": true, "hint": ...}` 信号，**不**返回空数组伪装「搜过没有」（禁 silent fallback；主 LLM 据 no_match 换关键词重试或细化任务）。
- 主 LLM 据路由结果选 → `call_skill` 派发。

新增事件 `skill_candidates_verified{verified_count, dropped_count}`（R3 可观测；`dropped_count` = 召回数 − 验证通过数，含「描述像但输入要求不满足」与无 body 的误召）。

### 决策四：recall / verify 双后端可插拔，默认 LLM-based，可换降本

`SkillRecall`（0023）与 `SkillVerifier`（本刀）都是 `runtime_checkable` Protocol，内核默认实现 `LlmSkillRecall` / `LlmSkillVerifier` 取依赖注入的 `ModelClient`（R1 不读环境变量、不绑定 provider）。业务可按成本 / 规模换：

- **降本**：注入 `KeywordSkillRecall`（零依赖、确定性召回）+ 仅 `LlmSkillVerifier`（只验前 N 个）→ 召回零 LLM、只验证花一次 LLM。
- **超大 pool**：召回换业务 RAG（ADR 0017③：内核只定协议、实现走外部）。

`LlmSkillVerifier` 的 C2 护栏：只验前 `verify_max_candidates`（默认 5）个、单 body 超 `verify_body_char_limit`（默认 4000 字符）截断（截断标记注入 reason）；整体失败抛 `SkillVerifyParseError`、单项脏数据丢弃不伪造；底层 LLM **非确定性**（replay / 测试靠固定 `model_client`）。

### 决策五：选择溯源用 verify_confidence（验证启用时）

派发时 `selection_confidence` = `search_skills` payload 的 `confidence`：验证启用时即 `verify_confidence`（适配置信）、未启用验证时即召回 `confidence`（长相）。沿用 0023 决策五 `selection_origin="discovered"`，零新增 schema。

## 成本取舍（显式声明）

| 配置 | 召回 LLM | 验证 LLM | 主选定 LLM | 适用 |
| --- | --- | --- | --- | --- |
| 默认（总闸关，`None`） | 零 | 零 | — | child 装得进 prompt，LLM 自己找（最便宜默认） |
| `enable_auto_discovery=True` | 一次 | 一次 | 一次 | 超量子 skill，要自动发现 + 误召过滤 |
| keyword 召回 + verify | 零 | 一次 | 一次 | 关键词区分度够、想省召回 LLM 的降本档 |

## 影响

- **R1 业务零侵入**：`SkillVerifier` 与 `SkillRecall` 同为业务注入缝；`src/` 内无业务概念；默认（总闸关）零额外依赖 / 零调用；`LlmSkillVerifier` 构造取业务提供的 `ModelClient`，不读环境变量、不绑定 provider。
- **R2 Cache 友好**：召回 / 验证均为 turn 内读路径，不改 prompt head、不返回 `CompressionResult`；deferred 判定仍是静态 prompt 形状（pre-turn 决定）。
- **R3 可观测**：新增 `skill_candidates_verified{verified_count, dropped_count}`，与 0023 的 `skill_search_invoked` / `skill_candidates_returned` 共同覆盖「召回 → 验证」全链。
- **R4 可取消**：`SkillVerifier.verify` 接收 `CancellationToken`，入口与 LLM 调用各传递；召回与验证之间再 check 一次。
- **R5 可 resume**：验证为读路径不落新持久化状态；溯源仍落进既有 `skill_outcome` JSONL（append-only）。

## 边界：明确不做的事

| 不做 | 原因 |
| --- | --- |
| 相位 4 跨白名单全局授权 | 召回 / 验证候选恒 ⊆ caller child 白名单（已预授权），本刀不碰授权边界——发现 / 适配 ≠ 准入 |
| 相位 5 fitness（战绩提拔 / 逐出） | fitness 依赖长期跨 session 战绩积累，与单 turn 的召回 / 验证不同相位；本刀不做 |
| 验证判「能否跑通」 | 能否跑通要真正派发执行才知道，验证只在派发前据 body 判输入要求适配（判适配，浅一层） |
| 默认开自动发现 | 违背最小惊讶 + 偷烧 LLM；做成 opt-in 总闸（决策一） |

## 落档

- 能力契约：[capabilities/skill-recall.md](../architecture/capabilities/skill-recall.md)（并入验证段：`VerifiedCandidate` / `SkillVerifier` / `LlmSkillVerifier` / `SkillVerifyParseError` 数据 + 行为契约 + 置信路由）。
- 模块活文档：[skill-system.md](../architecture/skill-system.md) §deferred 暴露与 search_skills 发现流。
- 旋钮：[configurable-knobs.md](../configurable-knobs.md)（`enable_auto_discovery` / `skill_verifier` / `verify_max_candidates` / `verify_body_char_limit`）。
- 能力登记：[capability-matrix.md](../capability-matrix.md)（Skill verification 行）。
