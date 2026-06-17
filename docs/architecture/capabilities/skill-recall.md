# skill-recall Specification

## Purpose

skill 发现 / 召回 —— 认知回路⑥「发现相位」的地基。

当 caller composite skill 的 `child_skills` 多到「装不进一次 prompt 的 inline 列表」时，内核不再把全部子 skill 内联列进 system prompt，而是改为 **deferred 暴露**：只给 LLM 一个 `search_skills(query)` 工具，让它据当前子任务意图**按需召回** top_k 个最相关的子 skill 候选，再据返回决定 `call_skill` 派发哪一个。

设计原则：**只召回、不准入、不分流**。相位 2 产出「据 query 在白名单内排出最相关候选」的结构化结果，把 `confidence` 作为数据透给 LLM 二次决策。相位 2 **不做**：据 confidence 自动放行 / 拦截 / 降级（分流是后续相位策略）；扩大召回作用域到白名单外（发现 ≠ 准入，授权边界不动）。

关联设计文档：`docs/superpowers/specs/2026-06-17-skill-recall-discovery-design.md`。
上游：`docs/superpowers/specs/2026-06-16-skill-capability-acquisition-loop-design.md`（§6 发现相位）。

## Requirements

### Requirement: deferred 暴露判定（inline / deferred 单一真相）

系统 SHALL 据 caller entry 的 `exposure.child_recall` 三值声明与 **G4 过滤后可见 child 数**，对「system prompt 是否内联列 child」与「per-turn 是否暴露 `search_skills` 工具」做**同一裁定**（`effective_child_recall`），两侧严格一致。

- `child_recall == "inline"` → 强制 `inline`（无论 child 多少全内联，**不**暴露 `search_skills`）。
- `child_recall == "deferred"` → 强制 `deferred`（无论 child 多少都走召回，暴露 `search_skills`）。
- `child_recall == "auto"`（默认）→ 可见 child 数 `> recall_threshold` 时 `deferred`，否则 `inline`。

`child_count` SHALL 传 **G4 过滤后的可见 child 数**（`visible_child_skills` 的结果），而非声明的原始 `child_skills` 总数。

#### Scenario: auto 小 entry 走 inline
- **GIVEN** auto entry 的可见 child 数 ≤ `recall_threshold`
- **THEN** per-turn 工具清单**不含** `search_skills`（向后兼容：原本就没有此工具）
- **AND** 内核恒备的 `read_skill` / `call_skill` 仍在

#### Scenario: auto 大 entry 走 deferred
- **GIVEN** auto entry 的可见 child 数 > `recall_threshold`
- **THEN** per-turn 工具清单**含** `search_skills`

#### Scenario: 显式声明优先于阈值
- **GIVEN** `child_recall: deferred` 的小 entry（child 远低于 threshold）
- **THEN** 仍暴露 `search_skills`
- **GIVEN** `child_recall: inline` 的大 entry（child 远超 threshold）
- **THEN** 仍不暴露 `search_skills`

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

### Requirement: 召回后端协议（SkillRecall）与默认实现

系统 SHALL 提供 `SkillRecall`（`typing.Protocol`，`runtime_checkable`）供业务侧注入自定义召回后端（关键词 / 向量 / 外部检索均可），并内置零依赖默认实现 `KeywordSkillRecall`。

实现方 SHALL 满足：
- **白名单封闭**：返回的每个 `skill_id` ⊆ `pool` 内 id 集。
- **数量上界**：`len(返回) ≤ top_k`。
- **置信合法**：每个候选 `confidence ∈ [0, 1]`。
- **纯函数 / 确定性**：相同 `(query, pool, top_k)` 给相同结果；**禁用系统时钟 / 随机源**（便于 replay / 测试）。
- **可取消**：收到 `cancel` 应尽早中断，不阻塞主 actor。

`KeywordSkillRecall` 用标准 BM25 公式（idf 加权 + tf 饱和 + 长度饱和归一）；零依赖分词（拉丁段按非字母数字边界、CJK 段字符 bigram）；同分按 `skill_id` 升序定序。

#### Scenario: isinstance 检测
- **WHEN** `isinstance(KeywordSkillRecall(), SkillRecall)`
- **THEN** SHALL 返回 `True`

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

### search_skills 工具 payload（透给 LLM）

`search_skills` 成功返回 `json.dumps(list[dict])`，每项**不含 score**（仅审计 / 内部）：

```python
{
    "skill_id":        str,
    "description":     str,
    "confidence":      float,        # [0,1]
    "matched_snippet": str | None,
}
```

入参 schema：`query`（必填，str）+ `top_k`（选填，int，缺省取 `recall_default_top_k`、超 `recall_max_top_k` 被钳制）。工具 `parallel_safe=True`（只读 snapshot + 召回）。

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

两事件均**不进 LLM 视图**，仅供 TelemetrySink / 审计消费。

## 行为契约

### deferred 暴露判定（`src/taifeng/skill/visibility.py::effective_child_recall`）

```
prompt 构建 + per-turn 工具裁剪 两侧都调 effective_child_recall(entry, child_count, threshold)
  ├─ child_recall == "inline"    → "inline"   （强制内联，无 search_skills）
  ├─ child_recall == "deferred"  → "deferred" （强制召回，暴露 search_skills）
  └─ child_recall == "auto"      → child_count > threshold ? "deferred" : "inline"
       child_count = len(visible_child_skills(entry, snapshot, capabilities))   # G4 过滤后可见数
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
  └─ ToolResult.ok(json([{skill_id, description, confidence, matched_snippet}]))   # 不外露 score
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
| **R1 业务零侵入** | `SkillRecall` Protocol 是业务注入缝（向量 / 外部检索替换默认）；`src/` 内无 tenant / 领域名词；`KeywordSkillRecall` 零依赖开箱可用，不注入也能跑 ✅ |
| **R2 Cache 友好** | inline / deferred 判定决定 entry **静态 system prompt 形状**（pre-turn 决定、整 turn 稳定），不是 mid-turn cache 失效，不返回 `CompressionResult`；同一 entry 跨 turn 走同一分支 → prefix 稳定 ✅ |
| **R3 可观测** | `skill_search_invoked` / `skill_candidates_returned` 两事件覆盖发现链关键路径，供 TelemetrySink 订阅 ✅ |
| **R4 可取消** | `SkillRecall.recall` 接收 `CancellationToken`，入口与打分后各 check 一次，尽早中断、不阻塞主 actor ✅ |
| **R5 可 resume** | 召回为读路径，不落新持久化状态；选择溯源仅 turn 内内存映射，溯源结果落进既有 `skill_outcome` JSONL（v1 append-only 路径），不依赖 engine 内存 resume ✅ |

## 边界与相位 2 明确不做的事

| 不做 | 原因 |
| --- | --- |
| 据 confidence 自动放行 / 拦截 / 降级 | 分流是后续相位策略；相位 2 只透数据给 LLM |
| 召回到白名单外 skill | 发现 ≠ 准入；授权边界（白名单 / DispatchPolicy）相位 2 不动 |
| 向量 / 外部检索后端 | 内核只定 `SkillRecall` 协议 + 零依赖关键词默认；向量 / RAG 是 userspace，业务自接 |
| 跨 session 召回统计 / 提拔 / 逐出 | 认知回路上层相位（fitness / 提拔），不在本契约范围 |
| score 持久化做阈值 | score 仅同次召回内可比，跨次无可比性 |
