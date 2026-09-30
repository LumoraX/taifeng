# Capability: skill-selection-gate

> 状态：Experimental（opt-in）。关联 ADR 0017（规则②）、0023、0024、0088；
> 设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 3。
> 实现：`src/taifeng/skill/selection.py`（分流策略、试用门协议、由 history 推导的判定，纯函数）、
> `src/taifeng/tool/builtins/selection_gate.py`（派发处的门）、`search_skills.py`（结果标注）。
> 经 `taifeng.experimental` 导出。

## Purpose

认知回路 ④ 评估 → ⑤ 试用：把召回候选的置信度变成**有约束力的分流**。相位 2 只把置信度交给模型自行掂量；
本能力让低置信候选不可直接派发、难分的候选必须先核实，结论由内核在派发处强制执行。

## 不变量

1. **只约束经发现选中的 skill**：模型在本轮里经 `search_skills` 看到它、且那份结果带分流结论。作者白名单里
   直接选中的子 skill、召回结果里没有出现的 skill SHALL NOT 过门。
2. **置信度只决定「要不要先试」**：分流结论 MUST NOT 进入战绩聚合与算分（`skill-working-set` 不变量 1 不变）。
3. **判定无状态**：放行与否只由当前 history 推导，runner 不持有分流状态；冷恢复后同一 history 得同一结论。
4. **默认不启用**：未注入 `SkillSelectionGate` 时，`search_skills` 输出与派发行为逐字节不变。
5. **不是权限边界**：分流门是认知质量门。越权防护由 `DispatchPolicy`（白名单 / 深度 / 环）与权限门负责，
   它们先于本门执行，本门不放宽其中任何一项。

## 数据契约

### 分流结论

`SelectionRoute = Literal["proceed", "trial", "escalate"]`

| 分流 | 含义 | 派发前的要求 |
| --- | --- | --- |
| `proceed` | 置信足够且与其他候选拉得开 | 无 |
| `trial` | 置信中等，或与另一个候选难分 | 先试用（见「派发门」） |
| `escalate` | 置信过低 | 本轮不可派发 |

### `SelectionCandidate` / `RoutedCandidate`

| 类型 | 字段 |
| --- | --- |
| `SelectionCandidate` | `skill_id`、`confidence`、`trust_tier`（来源信任层级，未知为 `None`） |
| `RoutedCandidate` | `skill_id`、`confidence`、`route`、`reason`（给模型看的英文短句） |

### `SelectionConfidencePolicy`（Protocol）与 `ThresholdSelectionPolicy`

`route(candidates) -> Sequence[RoutedCandidate]`；实现 SHALL 对每个候选返回一条结论、顺序不变。

默认实现 `ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0.05, trial_tiers=frozenset())`：

| 条件 | 分流 |
| --- | --- |
| `confidence < tau_low` | `escalate` |
| `tau_low <= confidence < tau_high` | `trial` |
| `confidence >= tau_high`，且与置信最高者之差 `< ambiguity_margin` 的候选不止一个 | 这些候选都为 `trial` |
| `confidence >= tau_high`，且 `trust_tier` 在 `trial_tiers` 里 | `trial` |
| 其余 `confidence >= tau_high` | `proceed` |

- `trial_tiers` 缺省为空；候选的 `trust_tier` 取自 `DispatchPolicy.trust`（见 [skill-working-set](skill-working-set.md)），
  未配置信任策略时为 `None`，不受 `trial_tiers` 影响。
- 并列判定只在置信 `>= tau_high` 的候选之间进行；`ambiguity_margin=0` 关闭并列判定。
- 阈值不在 `[0, 1]`、`tau_low > tau_high`、间距为负：构造期 `ValueError`。

### `TrialJudge`（Protocol）、`TrialVerdict`、`VerifierTrialJudge`

`async judge(*, task, skill_id, description, body, cancel) -> TrialVerdict(approved, reason)`。

- `task` 是当前 turn 的原始用户任务；`body` 是完整 SKILL.md 正文。
- 实现 MUST 可取消（R4）。异常不被吞：按工具故障上抛，由 tool runtime 统一落 `tool_error`。
- `VerifierTrialJudge(verifier)` 用既有 `SkillVerifier` 对单个候选做输入要求适配精验。

### `SkillSelectionGate`

| 字段 | 含义 |
| --- | --- |
| `policy` | 分流策略 |
| `trial_judge` | 试用门；`None`（默认）= `trial` 档要求模型自己先 `read_skill` |

## 行为契约

### 启用

| 入口 | 参数 |
| --- | --- |
| `EnginePool.create` | `selection_gate=SkillSelectionGate(...)`：同时作用于内置的 `search_skills` 与 `call_skill` |
| `make_search_skills_tool` | `selection_policy=` |
| `make_call_skill_tool` / `make_spawn_skill_tool` | `selection_gate=` |

`spawn_skill` 由业务经 `extra_tools` 注册，须自行传入同一个 `selection_gate`，否则分离派发不过门。

### `search_skills` 结果标注

启用后每个候选追加两个键，其余键不变：

```json
[{"skill_id": "...", "description": "...", "confidence": 0.62, "matched_snippet": null,
  "route": "trial", "route_reason": "confidence 0.62 is below 0.75"}]
```

全部候选都为 `escalate` 时返回显式 `no_match`，低置信候选列在 `low_confidence` 里：

```json
{"no_match": true, "hint": "...", "low_confidence": [{"skill_id": "...", "route": "escalate", "...": "..."}]}
```

启用了验证门（`skill_verifier`）时，参与分流的置信度是 `verify_confidence`；召回为空或验证全不适用的
`no_match` 分支不变。

### 派发门

`call_skill` / `spawn_skill` 在结构性准入之后、hook 与权限审批之前过门：

```
DispatchPolicy.check（白名单 / 深度 / 环）
  → 分流门
      ├─ 本轮没有带分流结论的召回结果提到该 skill → 放行（不打事件）
      ├─ proceed  → 放行
      ├─ escalate → 拦下 selection_low_confidence
      └─ trial
           ├─ 那次召回之后模型成功 read_skill 过它 → 放行（basis=read_skill）
           ├─ 未配置 trial_judge               → 拦下 selection_needs_trial
           └─ trial_judge.judge(...)
                ├─ approved → 放行（basis=trial_judge）
                └─ 否则     → 拦下 selection_trial_rejected（带试用门给的理由）
  → pre_skill_dispatch hook → 权限审批 → 派发
```

- 「本轮」从 history 里最后一条用户消息算起；上一轮的召回不约束本轮的派发。
- 同一轮内多次召回提到同一个 skill 时，以**最近一次**为准。
- 说明书读在那次召回**之前**不算试用。`escalate` 档读了说明书也不放行。
- 拦下的结果是 `is_error=True` 的工具结果，`data` 含 `reason`（`selection_needs_trial` /
  `selection_trial_rejected` / `selection_low_confidence`）、`skill_id`、`route`、`confidence`。
  被拦的派发不启动子 turn，不产生 `skill_outcome` 战绩。

### 失效方向

分流结论取自模型实际看到的那条工具结果。该结果被 hook 改写、被截断到不再是合法 JSON、或被压缩折叠后，
本门读不到结论，按「没有被召回过」放行。此时模型同样没有看到完整的候选列表，其选择不属于「经发现选中」。

## 事件

| kind | data | 时机 |
| --- | --- | --- |
| `skill_selection_routed` | `proceed` / `trial` / `escalate`（各档数量）、`routes`（skill_id → 分流） | `search_skills` 完成分流 |
| `skill_selection_gated` | `skill_id`、`call_id`、`route`、`confidence`、`admitted`、`basis` | 派发门对一个经发现选中的 skill 作出裁决 |

`basis` 取值：`route_proceed` / `read_skill` / `trial_judge`（放行）；`needs_trial` / `trial_rejected` /
`low_confidence`（拦下）。两个事件都不进 LLM 视图。事件投递失败只记日志，不改变裁决。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | 阈值、分流口径、试用门都是注入件；`src/` 内无业务概念 ✅ |
| **R2 Cache 友好** | 只改工具结果正文（history 尾部新增项）与派发裁决，不改 system prompt、不触发压缩 ✅ |
| **R3 可观测** | `skill_selection_routed` / `skill_selection_gated` 覆盖分流与裁决 ✅ |
| **R4 可取消** | `TrialJudge.judge` 接收 `CancellationToken`；分流策略是同步纯计算 ✅ |
| **R5 可 resume** | 不落新持久化状态；判定只读 history，冷恢复后结论一致 ✅ |

## 边界与明确不做的事

- 不改 inline 暴露下模型直接选中子 skill 的路径。
- 不做自动重搜：`escalate` 之后换关键词、报告没有匹配还是问用户，由模型决定。
- 不把分流结论写进 `SkillExecutionRecord`；战绩里的 `selection_confidence` 语义不变。
- 非白名单 skill 的授权属于相位 4，不在本能力内。
