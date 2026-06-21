# skill-recall Specification

## Purpose

skill 发现 / 召回 —— 认知回路⑥「发现相位」的地基。

当 caller composite skill 的 `child_skills` 多到「装不进一次 prompt 的 inline 列表」时，内核不再把全部子 skill 内联列进 system prompt，而是改为 **deferred 暴露**：只给 LLM 一个 `search_skills(query)` 工具，让它据当前子任务意图**按需召回** top_k 个最相关的子 skill 候选，再据返回决定 `call_skill` 派发哪一个。

设计原则：**只召回 + 验证适配、不准入、不据置信自动分流**。召回产出「据 query 在白名单内排出最相关候选」（看 `description`，长相）；其后可选的**验证门**（`SkillVerifier`，ADR 0024）拉**完整 SKILL.md body** 判「就当前任务的输入 / 条件，该能力声明要的输入是否满足、前提是否具备」（判**适配**，不判能否跑通），滤掉「描述像但输入要求不满足」的误召；最终把候选 + `confidence` 作为数据透给 LLM 二次决策。**不做**：据 confidence 自动放行 / 拦截 / 降级（分流是后续相位策略）；扩大召回 / 验证作用域到白名单外（发现 / 适配 ≠ 准入，授权边界不动）；判候选能否跑通（验证只判输入要求适配，浅一层）。

> **召回后端默认 = inline（工作记忆 / LLM 注意力）**：召回后端是一道「离工作记忆远近 = 成本」的阶梯——① **inline（默认，`skill_recall=None`）**：不注入任何后端时，全部可见 child 内联进 system prompt 由 LLM 自己选，**不**注册 `search_skills`、**不**启用 deferred；② **关键词 / BM25（`KeywordSkillRecall`）**：可选注入，零依赖；③ **LLM-as-recall（`LlmSkillRecall`）**：可选注入，一次性子 LLM 调用语义挑选；④ **向量 / RAG**：业务注入（ADR 0017③）。`search_skills` / deferred **仅在注入了召回后端时启用**——无后端**绝不**静默兜底关键词或暗启 search。详见下方「召回后端协议」与「行为契约」。

> **opt-in 自动发现总闸 `enable_auto_discovery`（默认 False，ADR 0024）**：要让「超量子 skill 自动走 LLM 召回 + 验证」开箱即用、又不改 `None=inline` 零成本默认，新增构造总闸 `enable_auto_discovery`。**关（默认）**：维持现状——`None=inline`、不验证、零额外 LLM。**开（True）**：在「未显式注入」处自动兜底——`skill_recall=None` 自动 `LlmSkillRecall(model_client)`、`skill_verifier=None` 自动 `LlmSkillVerifier(model_client)`，deferred 仍按 `recall_threshold` 伸缩。**显式注入优先于总闸**（显式后端 / verifier 直接启用，不依赖也不被总闸覆盖）；显式 `child_recall: deferred` 但既无注入又没开总闸 → 抛 `SkillValidationError`（禁 silent）。验证段（`SkillVerifier`）见下方「召回后验证（验证门）」。

关联设计文档：`docs/superpowers/specs/2026-06-18-skill-recall-verify-pipeline-design.md`（验证管线）、`docs/superpowers/specs/2026-06-17-skill-recall-discovery-design.md`（召回）。
上游：`docs/superpowers/specs/2026-06-16-skill-capability-acquisition-loop-design.md`（§6 发现相位）。

## Requirements

### Requirement: deferred 暴露判定（inline / deferred 单一真相）

系统 SHALL 据 caller entry 的 `exposure.child_recall` 三值声明、**G4 过滤后可见 child 数**与**是否注入了召回后端**（`has_recall_backend`），对「system prompt 是否内联列 child」与「per-turn 是否暴露 `search_skills` 工具」做**同一裁定**（`effective_child_recall`），两侧严格一致。

- `child_recall == "inline"` → 强制 `inline`（无论 child 多少全内联，**不**暴露 `search_skills`）。
- `child_recall == "deferred"` → 显式要召回：**有后端**则强制 `deferred`（暴露 `search_skills`）；**无后端**抛 `SkillValidationError`（启动期 fail-fast，禁 silent 降级回 inline——作者既然显式要召回就必须配后端）。
- `child_recall == "auto"`（默认）→ **有后端**且可见 child 数 `> recall_threshold` 时 `deferred`，否则 `inline`；**无后端恒 `inline`**（即便 child 很多，因为默认就是「LLM 自己找」）。

`child_count` SHALL 传 **G4 过滤后的可见 child 数**（`visible_child_skills` 的结果），而非声明的原始 `child_skills` 总数。`has_recall_backend` SHALL 反映 `skill_recall is not None`（默认 `None` = inline，无 `search_skills`）。

#### Scenario: 默认无后端恒 inline（即便 child 很多）
- **GIVEN** 未注入召回后端（`skill_recall=None`，默认），entry 为 `auto`
- **WHEN** 可见 child 数远超 `recall_threshold`
- **THEN** 仍走 `inline`，per-turn 工具清单**不含** `search_skills`（默认 = LLM 从全列里自己找；海量场景由业务注入后端）

#### Scenario: auto 小 entry 走 inline
- **GIVEN** 注入了召回后端的 auto entry 的可见 child 数 ≤ `recall_threshold`
- **THEN** per-turn 工具清单**不含** `search_skills`（向后兼容：原本就没有此工具）
- **AND** 内核恒备的 `read_skill` / `call_skill` 仍在

#### Scenario: auto 大 entry 走 deferred（须有后端）
- **GIVEN** 注入了召回后端的 auto entry 的可见 child 数 > `recall_threshold`
- **THEN** per-turn 工具清单**含** `search_skills`

#### Scenario: 显式声明优先于阈值
- **GIVEN** 注入了后端、`child_recall: deferred` 的小 entry（child 远低于 threshold）
- **THEN** 仍暴露 `search_skills`
- **GIVEN** `child_recall: inline` 的大 entry（child 远超 threshold）
- **THEN** 仍不暴露 `search_skills`

#### Scenario: 显式 deferred 但无后端 → 启动期抛错
- **GIVEN** `child_recall: deferred` 的 entry，但未注入任何召回后端
- **WHEN** 裁定 `effective_child_recall`
- **THEN** 抛 `SkillValidationError`（fail-fast，**不**静默降级回 inline）

#### Scenario: 作者显式声明 search_skills 不重复
- **GIVEN** deferred entry 在 SKILL.md `tool_names` 显式声明了 `search_skills`
- **WHEN** 内核组装 per-turn 工具清单（deferred 分支想追加 `search_skills`）
- **THEN** `search_skills` 在清单里**恰好出现一次**（按已加入工具名集合去重）

### Requirement: 召回作用域 = 白名单内 G4 过滤（同源同过滤）

系统 SHALL 把召回语料池限定为 **caller entry 的 `child_skills` 经 G4 过滤后的可见集**，与 `loop/prompt.py` 的 inline 列表用**同一套**过滤（`visible_child_skills`），且白名单封闭由**内核**钉死——召回后端只能在传入的 `pool` 内排名，无从越权召回池外 skill。

- 召回池**仅含 caller 的 `child_skills`**（更窄），不是 reachable 全集——召回是「为 LLM 选下一个 call_skill 目标」服务，目标必须在白名单内。
- **G4b**：`exposure.model_invocable == False` 的子 skill 不进池（与 inline 一致）。
- **G4a**：提供 `RuntimeCapabilities` 时，`requires` 不满足的子 skill 不进池（与 inline 一致）。
- 召回**不触碰授权 / 准入**：发现一个 skill ≠ 有权派发它；准入仍由 `DispatchPolicy`（深度 / 环 / 白名单）在 `call_skill` 派发时裁决。

#### Scenario: deferred 不构成 G4 旁路
- **GIVEN** caller 的某 child `model_invocable=False` 或 `requires` 不满足
- **WHEN** LLM 调 `search_skills` 召回
- **THEN** 该 child **不**出现在召回候选中（与 inline 列表对该 child 的隐藏一致）

### Requirement: 召回置信仅透数据、不分流

系统 SHALL 把 `SkillCandidate.confidence`（同次召回内 score 归一到 [0,1]）作为**数据**透给 LLM 二次决策，相位 2 内核任何路径 SHALL **不**据 confidence 自动放行 / 拦截 / 降级派发。

- `score`（后端原始相关度）**仅同次召回内可比**，禁止跨次比较 / 持久化做阈值；透给 LLM 的 payload **不外露 score**，只给 `confidence` / `matched_snippet`。
- 是否据 confidence 分流属后续相位策略，本契约不承诺任何此类语义。

### Requirement: 选择溯源连回 v1 战绩

系统 SHALL 在「经 `search_skills` 召回选中的 skill 被 `call_skill` 派发」时，把该派发的战绩记录（[skill-outcome-record](skill-outcome-record.md)）标 `selection_origin="discovered"` + `selection_confidence=<召回 confidence>`；未经召回的派发仍为 v1 的 `whitelist` / `None`。

- 复用 v1 已有的 `SelectionOrigin` Literal（`"whitelist"` | `"discovered"`），不新增字段。
- 溯源是 turn 内 best-effort：search_skills 返回候选时登记 `(skill_id → ("discovered", confidence))`；同 id 后到的召回**覆盖**先到的（最近一次最贴近「LLM 当前据以决策」）。
- 容错（非 silent fallback）：search_skills output 结构异常（坏 JSON / 缺字段）时**跳过该项**、不伪造默认 confidence，退化为 v1 的 `whitelist` / `None`。

#### Scenario: 召回选中派发记 discovered
- **GIVEN** LLM 调 `search_skills` 召回出候选 `X`（confidence=c），随后 `call_skill(X)`
- **THEN** `X` 的 `SkillExecutionRecord.selection_origin == "discovered"`
- **AND** `selection_confidence == c`

#### Scenario: 未经召回派发记 whitelist
- **GIVEN** LLM 直接 `call_skill(Y)`，本 turn 未曾召回出 `Y`
- **THEN** `Y` 的 `selection_origin == "whitelist"`，`selection_confidence == None`

### Requirement: 召回后端协议（SkillRecall）+ 阶梯实现（默认 inline、后端可选注入）

系统 SHALL 提供 `SkillRecall`（`typing.Protocol`，`runtime_checkable`）供业务侧注入召回后端（关键词 / LLM / 向量 / 外部检索均可）。

**内核默认 `skill_recall=None` = inline（工作记忆 / LLM 注意力）**，不注入任何后端、不注册 `search_skills`、不启用 deferred。内核提供两个可选注入实现：

| 阶梯 | 实现 | 确定性 | 依赖 | 适用 |
| --- | --- | --- | --- | --- |
| ① 工作记忆 / LLM 注意力（**默认**） | inline（`skill_recall=None`） | — | 零 | child 装得进一次 prompt，LLM 自己找 |
| ② 关键词 / BM25 | `KeywordSkillRecall`（可选注入） | 确定性 | 零依赖 | child 多到装不进 prompt，关键词区分度够 |
| ③ LLM-as-recall | `LlmSkillRecall`（可选注入，本次新增） | **非确定性** | 一次性子 LLM 调用 | 描述语义复杂、关键词不足且 pool 仍能放进一次 prompt |
| ④ 向量 / RAG | 业务注入（ADR 0017③） | 视实现 | 外部检索服务 | 万级 skill / 高密度语义近邻 |

纯算法后端（如 `KeywordSkillRecall`）SHALL 满足：
- **白名单封闭**：返回的每个 `skill_id` ⊆ `pool` 内 id 集。
- **数量上界**：`len(返回) ≤ top_k`。
- **置信合法**：每个候选 `confidence ∈ [0, 1]`。
- **纯函数 / 确定性**：相同 `(query, pool, top_k)` 给相同结果；**禁用系统时钟 / 随机源**（便于 replay / 测试）。
- **可取消**：收到 `cancel` 应尽早中断，不阻塞主 actor。

`KeywordSkillRecall` 用标准 BM25 公式（idf 加权 + tf 饱和 + 长度饱和归一）；零依赖分词（拉丁段按非字母数字边界、CJK 段字符 bigram）；同分按 `skill_id` 升序定序。

`LlmSkillRecall`（**非确定性后端**，不满足上面「纯函数 / 确定性」那条——这是 LLM 召回的固有性质，replay / 测试靠固定 `model_client`（如 SimClient）脚本化回放）SHALL 满足前三条（白名单封闭 / 数量上界 / 置信合法）+ 可取消，详见下方「LlmSkillRecall」数据契约。

#### Scenario: isinstance 检测
- **WHEN** `isinstance(KeywordSkillRecall(), SkillRecall)` 或 `isinstance(LlmSkillRecall(client), SkillRecall)`
- **THEN** SHALL 返回 `True`

#### Scenario: 默认无后端 = inline
- **GIVEN** `EnginePool.create(..., skill_recall=None)`（默认）
- **THEN** 不注册 `search_skills`、所有 entry 恒走 inline；无任何召回调用发生

### Requirement: 自动发现 opt-in 总闸（enable_auto_discovery）

系统 SHALL 提供构造总闸 `enable_auto_discovery: bool`（`EnginePool.create` / `EnginePool.__init__`，默认 `False`），让「超量子 skill 自动走 LLM 召回 + 验证」开箱即用，且 SHALL **不**改变 `skill_recall=None=inline` 的零成本默认语义。

- **关（默认 False）**：`skill_recall=None` 走 inline（LLM 自己找）、`skill_verifier=None` 不验证；**零额外 LLM 调用**。
- **开（True）**：在「未显式注入」处自动兜底——`skill_recall is None` 自动用 `LlmSkillRecall(model_client)`、`skill_verifier is None` 自动用 `LlmSkillVerifier(model_client)`；deferred 判定照旧按 `recall_threshold` 伸缩。
- **显式注入优先于总闸**：业务显式传 `KeywordSkillRecall` / 业务 RAG / `LlmSkillRecall` → 直接启用（**不依赖**总闸、也**不被**总闸覆盖）；显式传 `skill_verifier` → 直接启用验证（与 recall 正交：可「keyword 召回 + LLM 验证」）。
- **禁 silent**：显式 `child_recall: deferred` 却**既无注入又没开总闸** → 抛 `SkillValidationError`（启动期 fail-fast）。`has_recall_backend` 在总闸开启后即为真（兜底实例非 None）。

#### Scenario: 总闸开启自动补 LLM 召回 + 验证
- **GIVEN** `EnginePool.create(..., enable_auto_discovery=True)`，未显式注入 `skill_recall` / `skill_verifier`
- **THEN** 召回后端解析为 `LlmSkillRecall(model_client)`、验证后端解析为 `LlmSkillVerifier(model_client)`；`has_recall_backend` 为真，超 `recall_threshold` 的 auto entry 走 deferred

#### Scenario: 显式注入不被总闸覆盖
- **GIVEN** `enable_auto_discovery=True` 且显式 `skill_recall=KeywordSkillRecall()`
- **THEN** 召回用注入的 `KeywordSkillRecall`（非 `LlmSkillRecall`）；验证仍自动补 `LlmSkillVerifier`（「keyword 召回 + LLM 验证」正交组合）

### Requirement: 召回后验证（验证门 SkillVerifier）

系统 SHALL 在召回**之后**提供一道可插拔验证门：拉**完整 SKILL.md body**，判「就当前任务能提供的输入 / 条件，该能力**声明要的输入是否满足、前提是否具备**」（判**适配**，**不**判能否跑通），把「描述像但输入要求不满足」的误召滤掉。验证与召回**正交**——召回看 `description`（长相，浅），验证看 body（适配）。

- **长相与适配分字段**（防呆）：`VerifiedCandidate` 把 `recall_confidence`（召回长相）与 `verify_confidence`（验证适配）**分开存**，语义不同、不可混用；溯源 / 路由用 `verify_confidence`，**不**拿 `recall_confidence` 喂提拔。
- **只返回适用**：验证后端返回列表**仅含 `applicable=True`** 的候选（不适用的滤掉）。
- **候选封闭**：返回的每个 `skill_id` 必 ⊆ 入参 `candidates`；**不得**自取 registry snapshot——body 一律经调用方提供的 `get_body(skill_id)` 取（控制权钉在调用方）。
- **置信合法**：每个候选 `verify_confidence ∈ [0,1]`。
- **可取消**：`verify` 接收 `CancellationToken`，尽早中断（R4）。

`LlmSkillVerifier`（默认实现，**非确定性**——底层 LLM，replay / 测试靠固定 `model_client`，但返回**排序确定**：`verify_confidence` 降序 / 同分 `skill_id` 升序）SHALL 满足上述全部约束，外加 C2 护栏：

- **只验前 N 个**：召回宽筛（top_k 可几十），验证只对前 `verify_max_candidates`（默认 5）个精验（控成本 / prompt 体积）。
- **单 body 超限截断**：单 body 超 `verify_body_char_limit`（默认 4000 字符）截前缀进 prompt，并在该候选 `reason` 追加截断标记（审计可见）。
- **无 body 不送 LLM**：`get_body` 返回 `None` 的候选直接判不适用、不送 LLM（既不浪费 token 也不伪造结论），按「仅返回 applicable=True」过滤后自然不出现。
- **整体失败抛 `SkillVerifyParseError`**：模型回答非合法 JSON / 顶层非数组 / provider 在流中 emit error → 抛错（显式失败，禁 silent 伪造默认结论）。
- **单项脏数据丢弃**：单项非对象 / `skill_id` ∉ 已验池 / 缺 / 非法 `applicable`（非 bool）/ `confidence`（非数值或越界 `[0,1]`）/ `reason`（缺 / 空）→ 丢弃该项（不抛错、不伪造）。
- `description` / `recall_confidence` 取自入参 candidate（不信模型复述）。

#### Scenario: isinstance 检测
- **WHEN** `isinstance(LlmSkillVerifier(client), SkillVerifier)`
- **THEN** SHALL 返回 `True`

#### Scenario: 误召被验证滤掉
- **GIVEN** 召回出候选 `X`（描述贴切），但 `X` 正文声明要一份当前任务给不了的输入
- **WHEN** 验证据完整 body 判 `X.applicable=False`
- **THEN** `X` **不**出现在验证结果里；`dropped_count` 计入该项

#### Scenario: 全不适用走显式 no_match
- **GIVEN** 召回了候选但验证后全部 `applicable=False`（或召回本就空）
- **THEN** `search_skills` 返回 `{"no_match": true, "hint": ...}`（**不**返回空数组伪装）

### Requirement: 验证启用时 search_skills 置信路由（禁 silent）

启用验证（注入 `skill_verifier` 或开 `enable_auto_discovery`）时，`search_skills` 流程 SHALL 为「召回 → 验证 → 置信路由」，且路由 SHALL **不** silent fallback：

- **有 applicable 候选** → 返回 `[{skill_id, description, confidence, reason}]`，`confidence` 键名**沿用**（值 = `verify_confidence`，保 turn 溯源自然读取不打断）；启用验证时**不再带** `matched_snippet`（以验证结果为准）。
- **全不适用 / 召回空** → 返回**显式** `{"no_match": true, "hint": ...}` 信号。
- **召回与验证之间** SHALL 再 check 一次取消（R4：长链路尽早中断）。
- 未启用验证时退化为 0023 行为（召回直接路由，payload 含 `matched_snippet`）。

- **验证用「原始任务」而非召回 query（关键契约）**：`search_skills` SHALL 把**当前 turn 的原始用户任务**（`ToolContext.extras["current_task"]`，由 `TurnRunner` 注入最近一条 `user_message` 文本）作为 `verifier.verify(task=...)` 的 `task`，**而非** LLM 写的关键词 `query`；裸调用 / 老上下文缺 `current_task` 时回退 `query`（保旧路径不崩）。**理由**：召回需要为词面匹配优化、按 schema 指引刻意剥离口语输入上下文的关键词 `query`；但验证要判「当前任务给的输入是否满足要求」，必须看到「附件是发票照片」这类**输入信号**——若复用关键词 query，输入上下文被剥离，验证会把有效候选误判 `applicable=False`。即**召回与验证输入语义不同，不可共用一个 query**。`recall.recall(query, ...)` 仍用 `query`（关键词匹配不变）。

#### Scenario: 验证溯源用 verify_confidence
- **GIVEN** 验证启用，候选 `X` 经验证（`verify_confidence=v`）后被 `call_skill(X)` 派发
- **THEN** `X` 的 `SkillExecutionRecord.selection_origin == "discovered"`，`selection_confidence == v`（适配置信，非召回长相）

## Data Contract

### SkillCandidate（`src/taifeng/skill/recall.py`）

一条召回候选：把「后端原始相关度」与「归一化置信」分开存。

```python
@dataclass(frozen=True)
class SkillCandidate:
    skill_id:        str          # 候选 skill id（必须 ⊆ 召回时传入的 pool）
    description:     str          # 候选 skill 描述（透给 LLM 做二次决策）
    score:           float        # 后端原始相关度，≥0，仅同次召回内可比（禁跨次比较 / 持久化阈值）
    confidence:      float        # score 同池归一到 [0,1] 的相对置信（仅透数据、不分流）
    matched_snippet: str | None   # 命中片段（审计「为何被召回」）；无则 None，禁用空串伪装
```

### RecallEntry（召回语料池一项）

内核按 caller 白名单解析出可见 skill 集后，把每个包成 `RecallEntry` 传给后端；后端只能在此池内排名（白名单封闭钉在内核手里）。

```python
@dataclass(frozen=True)
class RecallEntry:
    skill_id:    str   # skill id
    description: str   # skill 描述（召回后端据此匹配相关性）
```

### SkillRecall（可插拔召回后端协议）

```python
@runtime_checkable
class SkillRecall(Protocol):
    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        ...
```

### LlmSkillRecall（可选注入的 LLM-as-recall 后端，`src/taifeng/skill/recall.py`）

构造取依赖注入的 `ModelClient`（业务侧提供，决定 provider / 鉴权；R1 不读环境变量、不绑定 provider）；`recall` 起一次性子 LLM 调用，把**整个 pool** 的 `skill_id + description` 拼成召回 prompt，要求模型据 query 语义按相关度降序输出 JSON 数组，再解析回 `SkillCandidate` 列表。

```python
class LlmSkillRecall:
    def __init__(self, model_client: ModelClient, *, model: str | None = None) -> None: ...
    async def recall(
        self, query: str, pool: Sequence[RecallEntry], *, top_k: int, cancel: CancellationToken,
    ) -> list[SkillCandidate]: ...
```

约束与解析策略：

- **非确定性**：底层是 LLM，相同 `(query, pool)` 不保证相同结果——故**不满足** `SkillRecall` 协议对纯算法后端的「纯函数 / 确定性」要求；replay / 测试靠固定 `model_client`（SimClient）脚本化回放。
- **pool 须能放进一次 prompt**：本后端把整个 pool 列进 prompt，**不做**分片 / 召回的召回；万级 skill 撑爆 context 的场景应交给向量检索 / 外部 RAG（业务实现同协议注入）。
- **整体失败抛 `SkillRecallParseError`**：模型回答非合法 JSON、或顶层不是数组、或 provider 在流中 emit error → 抛 `SkillRecallParseError`（显式失败，**禁** silent fallback 伪造默认候选）。
- **单项脏数据丢弃（不伪造）**：单项 `skill_id` ∉ pool / 缺 `skill_id` / 缺 `score` / `score` 非数值或越界 `[0,1]` → **丢弃该项**（整批可解析、个别项不合法属正常，不抛错、不越权召回池外 skill）。
- `description` 一律取自 pool（不信模型复述）；`matched_snippet` 恒为 `None`（LLM 召回无确定命中片段，禁用空串伪装）；结果按模型给出的 `score` 降序、截断到 `top_k`。
- **可取消**：`recall` 接收 `CancellationToken` 并透传给 LLM session。

### SkillRecallParseError（`src/taifeng/skill/recall.py`）

`LlmSkillRecall` 无法把模型回答整体解析为合法召回结果（坏 JSON / 顶层非数组 / provider 调用失败）时抛出。单项级脏数据**不**抛本异常（按上「丢弃该项」处理）。

### VerifiedCandidate（`src/taifeng/skill/verify.py`）

验证后的候选：在召回基础上加「输入要求是否满足」的结论。**长相与适配分字段**（防呆）。

```python
@dataclass(frozen=True)
class VerifiedCandidate:
    skill_id:          str    # 候选 id（必 ⊆ 入参 candidates）
    description:       str    # 候选描述（取自入参 candidate，不信模型复述）
    recall_confidence: float  # 召回阶段相关度（长相，浅）——来自 SkillCandidate.confidence
    applicable:        bool   # 就当前任务输入，是否满足该能力声明要的输入 / 前提
    verify_confidence: float  # 验证阶段适配置信 ∈ [0,1]（与长相分离的独立字段）
    reason:            str    # 为何适用 / 不适用（审计追溯；body 截断会在此注明标记）
```

### SkillVerifier（可插拔验证后端协议，`src/taifeng/skill/verify.py`）

```python
@runtime_checkable
class SkillVerifier(Protocol):
    async def verify(
        self,
        task: str,                                   # = search 的 query
        candidates: Sequence[SkillCandidate],        # 召回回来的候选（仅 description 级）
        *,
        get_body: Callable[[str], str | None],       # 按 skill_id 取完整 body；取不到 None
        cancel: CancellationToken,
    ) -> list[VerifiedCandidate]:                     # 仅含 applicable=True
        ...
```

约束：候选封闭（`skill_id` ⊆ candidates）/ 只返回 applicable=True / `verify_confidence ∈ [0,1]` / body 一律经 `get_body`（实现方不得自取 snapshot）/ 可取消（R4）。

### LlmSkillVerifier（默认验证实现，`src/taifeng/skill/verify.py`）

构造取依赖注入的 `ModelClient`（R1 不读环境变量、不绑定 provider）；起一次性子 LLM 调用，把前 `verify_max_candidates` 个候选的 `skill_id + 完整 body`（超 `verify_body_char_limit` 截断）拼成验证 prompt，要求模型逐个判输入要求适配并输出 JSON，再解析回 `VerifiedCandidate`、**只保留 applicable=True**。

```python
class LlmSkillVerifier:
    def __init__(
        self,
        model_client: ModelClient,
        *,
        model: str | None = None,
        verify_max_candidates: int = 5,       # C2 护栏：只精验召回头部前 N 个
        verify_body_char_limit: int = 4000,   # C2 护栏：单 body 入 prompt 字符上限
    ) -> None: ...
    async def verify(
        self, task: str, candidates: Sequence[SkillCandidate], *,
        get_body: Callable[[str], str | None], cancel: CancellationToken,
    ) -> list[VerifiedCandidate]: ...
```

约束与解析策略：

- **非确定性**：底层 LLM，相同 `(task, candidates)` 不保证相同结果；replay / 测试靠固定 `model_client`（SimClient）。但返回**排序确定**（`verify_confidence` 降序 / 同分 `skill_id` 升序）。
- **body 须能进一次 prompt**：本后端把精验候选 body 全列进一次 prompt；护栏已限（前 N 个 + 单 body 截断）；万级超大场景交外部服务（业务实现同协议）。
- **无 body 不送 LLM**：`get_body` 返回 `None` 的候选直接判不适用、不送 LLM（不浪费 token、不伪造），过滤后自然不出现在结果里。
- **整体失败抛 `SkillVerifyParseError`**：非合法 JSON / 顶层非数组 / provider error → 抛错（禁 silent）。
- **单项脏数据丢弃**：单项 `skill_id` ∉ 已验池 / 缺 / 非法 `applicable`（非 bool）/ `confidence`（非数值或越界）/ `reason`（缺 / 空）→ 丢弃该项（不伪造）。
- `description` / `recall_confidence` 取自入参 candidate；body 截断时 `reason` 追加截断标记；空候选 / 全无 body 短路返回 `[]`（不发 LLM）。
- **可取消**：`verify` 入口与 LLM 调用均传递 `CancellationToken`。

### SkillVerifyParseError（`src/taifeng/skill/verify.py`）

`LlmSkillVerifier` 无法把模型回答整体解析为合法验证结果（坏 JSON / 顶层非数组 / provider 调用失败）时抛出。单项级脏数据**不**抛本异常（按上「丢弃该项」处理）。

### search_skills 工具 payload（透给 LLM）

`search_skills` 成功返回 `json.dumps(...)`。**形状依是否启用验证而分两路**（禁 silent fallback）：

**未启用验证**（无 verifier、总闸关）—— 召回直接路由，每项**不含 score**：

```python
{
    "skill_id":        str,
    "description":     str,
    "confidence":      float,        # [0,1]，召回归一置信（长相）
    "matched_snippet": str | None,
}
```

**启用验证**（注入 `skill_verifier` 或 `enable_auto_discovery=True`）—— 召回 → 验证 → 置信路由：

- 有 applicable 候选 → `list[dict]`，每项 `{skill_id, description, confidence, reason}`；`confidence` 键名沿用（值 = `verify_confidence`），**不带** `matched_snippet`。
- 全不适用 / 召回空 → 显式 `{"no_match": true, "hint": str}` 对象（不返回空数组伪装）。

入参 schema：`query`（必填，str）+ `top_k`（选填，int，缺省取 `recall_default_top_k`、超 `recall_max_top_k` 被钳制）。工具 `parallel_safe=True`（只读 snapshot + 召回 + 验证）。

### SkillSearchInvoked 事件（`src/taifeng/loop/event.py`）

```python
class SkillSearchInvoked(_Msg):
    kind: Literal["skill_search_invoked"] = "skill_search_invoked"
    # data = {"query": str, "top_k": int, "pool_size": int}
    #   pool_size = G4 过滤后的可见池规模
```

### SkillCandidatesReturned 事件（`src/taifeng/loop/event.py`）

```python
class SkillCandidatesReturned(_Msg):
    kind: Literal["skill_candidates_returned"] = "skill_candidates_returned"
    # data = {"count": int, "top_ids": list[str]}  # top_ids 按相关度排序
```

### SkillCandidatesVerified 事件（`src/taifeng/loop/event.py`，验证启用时）

```python
class SkillCandidatesVerified(_Msg):
    kind: Literal["skill_candidates_verified"] = "skill_candidates_verified"
    # data = {"verified_count": int, "dropped_count": int}
    #   verified_count = 验证通过（applicable=True）保留的候选数
    #   dropped_count  = 召回数 − 验证通过数（含「描述像但输入要求不满足」与无 body 的误召）
```

三事件均**不进 LLM 视图**，仅供 TelemetrySink / 审计消费。`skill_candidates_verified` 仅在启用验证时 emit。

## 行为契约

### deferred 暴露判定（`src/taifeng/skill/visibility.py::effective_child_recall`）

```
prompt 构建 + per-turn 工具裁剪 两侧都调 effective_child_recall(entry, child_count, threshold, has_recall_backend)
  ├─ child_recall == "inline"    → "inline"   （强制内联，无 search_skills）
  ├─ child_recall == "deferred"  → has_recall_backend ? "deferred" : raise SkillValidationError
  │                                 （显式要召回但无后端 → fail-fast，禁 silent 降级 inline）
  └─ child_recall == "auto"      → (has_recall_backend and child_count > threshold) ? "deferred" : "inline"
       child_count = len(visible_child_skills(entry, snapshot, capabilities))   # G4 过滤后可见数
       has_recall_backend = (skill_recall is not None)                          # 默认 None=inline，无 search_skills
```

### 召回链（`src/taifeng/tool/builtins/search_skills.py`）

```
LLM 调 search_skills(query, top_k?)
  ├─ 校验 query（缺失 / 非 str → bad_args）
  ├─ clamp top_k（缺省→default，越界→钳到 max）
  ├─ 从 ctx.extras 取 skill_snapshot / current_skill（缺→config_error）+ capabilities（可选）
  ├─ 构召回池：visible_child_skills(caller, snapshot, capabilities) → list[RecallEntry]
  ├─ emit SkillSearchInvoked(query, top_k, pool_size)
  ├─ recall.recall(query, pool, top_k, cancel)   # 白名单封闭：pool 即可召回全集
  ├─ emit SkillCandidatesReturned(count, top_ids)
  ├─ 未启用验证 → ToolResult.ok(json([{skill_id, description, confidence, matched_snippet}]))  # 不外露 score
  └─ 启用验证（注入 verifier 或开总闸）：
       ├─ ctx.cancel.raise_if_cancelled()                       # 召回↔验证之间再 check（R4）
       ├─ get_body = λ sid: snapshot.get(sid).body              # body 取道唯一
       ├─ verifier.verify(query, candidates, get_body, cancel)  # 仅 applicable=True
       ├─ emit SkillCandidatesVerified(verified_count, dropped_count=召回数−验证通过数)
       └─ 置信路由（禁 silent）：
            ├─ 有 applicable → ToolResult.ok(json([{skill_id, description, confidence(=verify), reason}]))
            └─ 全不适用 / 空 → ToolResult.ok(json({"no_match": true, "hint": ...}))
```

### per-turn 工具裁剪（`src/taifeng/loop/turn.py`）

`search_skills` 在 `pool.create` 时**全局注册**，但只在 `_deferred_exposure_active()`（即 `effective_child_recall == "deferred"`）的 entry 上暴露；append 前按已加入工具名集合**去重**（作者在 `tool_names` 显式声明 `search_skills` 时不重复）。

### 选择溯源（`src/taifeng/loop/turn.py::_register_selection_trace`）

```
search_skills 成功完成 → 解析候选 JSON → 登记 turn 内 {skill_id: ("discovered", confidence)}
  ...
call_skill(target) 派发 → 查 turn 内溯源映射
  ├─ 命中 → SkillExecutionRecord.selection_origin="discovered" + selection_confidence=confidence
  └─ 未命中 → v1 行为：selection_origin="whitelist" + selection_confidence=None
```

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | `SkillRecall` Protocol 是业务注入缝（关键词 / LLM / 向量 / 外部检索按规模选注）；`src/` 内无 tenant / 领域名词；默认 inline 零依赖开箱可用，不注入也能跑；`LlmSkillRecall` 构造取业务提供的 `ModelClient`（不读环境变量、不绑定 provider）✅ |
| **R2 Cache 友好** | inline / deferred 判定决定 entry **静态 system prompt 形状**（pre-turn 决定、整 turn 稳定），不是 mid-turn cache 失效，不返回 `CompressionResult`；同一 entry 跨 turn 走同一分支 → prefix 稳定 ✅ |
| **R3 可观测** | `skill_search_invoked` / `skill_candidates_returned` 覆盖召回链；启用验证时追加 `skill_candidates_verified{verified_count, dropped_count}` 覆盖验证门，供 TelemetrySink 订阅 ✅ |
| **R4 可取消** | `SkillRecall.recall` / `SkillVerifier.verify` 均接收 `CancellationToken`；召回入口、召回↔验证之间、验证入口与 LLM 调用各 check，尽早中断、不阻塞主 actor ✅ |
| **R5 可 resume** | 召回为读路径，不落新持久化状态；选择溯源仅 turn 内内存映射，溯源结果落进既有 `skill_outcome` JSONL（v1 append-only 路径），不依赖 engine 内存 resume ✅ |

## 边界与明确不做的事

| 不做 | 原因 |
| --- | --- |
| 据 confidence 自动放行 / 拦截 / 降级（召回置信） | 分流是策略；召回相位只透数据给 LLM |
| 召回 / 验证到白名单外 skill | 发现 / 适配 ≠ 准入；候选恒 ⊆ caller child 白名单，授权边界（DispatchPolicy）不动 |
| 验证判「能否跑通」 | 验证只在派发前据 body 判**输入要求适配**（判适配，不判能否跑通）；能否跑通要真正派发执行才知道 |
| 默认开自动发现 | 违背最小惊讶 + 偷烧 LLM；做成 opt-in 总闸 `enable_auto_discovery`（默认 False，ADR 0024） |
| 相位 4 跨白名单全局授权 / 相位 5 fitness（提拔 / 逐出） | 跨 session 战绩 / 授权属认知回路上层相位，不在本契约范围 |
| 向量 / 外部检索后端 | 内核只定 `SkillRecall` / `SkillVerifier` 协议，默认 LLM-based + 可选注入；向量 / RAG 是 userspace，业务自接 |
| score 持久化做阈值 | score 仅同次召回内可比，跨次无可比性 |
