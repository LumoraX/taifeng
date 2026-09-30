# Capability: skill-working-set

> 状态：Experimental（opt-in）。关联 ADR 0017（规则②）、0067、0077、0090；
> 设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 5。
> 实现：`src/taifeng/skill/working_set.py`（算分 + 规划，纯函数）、`src/taifeng/skill/fitness_shadow.py`
> （影子评估）、`src/taifeng/skill/working_set_runtime.py`（生效）、`src/taifeng/skill/trust.py`
> （来源信任分层）、`src/taifeng/skill/fitness.py`（聚合）。经 `taifeng.experimental` 导出。

## Purpose

认知回路 ⑦ 沉淀相位的决策部分：按**真实执行结果**给 skill 算分，规划哪些 skill 值得常驻工作记忆、哪些
「描述过度承诺」该被隔离。两种用法，互不依赖：

| 用法 | 组件 | 效果 |
| --- | --- | --- |
| 影子 | `SkillFitnessShadow` | 算分、规划、记录结论，不改变任何 skill 的可见性、排序、召回或派发 |
| 生效 | `SkillWorkingSet` | 结论作用到 system prompt、child 列表、召回池与派发 |

上线顺序：先挂影子积累数据、核对结论，再启用生效。

## 不变量

1. **长相不得喂战绩**：算分与规划 MUST NOT 读取 `selection_confidence`。聚合里的 `discovered_selections`
   只是「经发现被选中的次数」，不含置信度。
2. **没真干成过就不涨分**：没有成败样本的 skill 得 0 分。
3. **放弃不算失败**：`abandoned`（取消 / 人拒绝）不进成功率分母。
4. **影子不生效**：内核的 prompt 组装、召回、派发路径 SHALL NOT 持有影子评估组件的引用。
5. **生效的结论在 turn 开始时取定**：一个 turn 内 system prompt、child 列表、召回池用同一份快照，
   SHALL NOT 在 turn 中途改变。
6. **来源信任只调门槛，不改战绩分**：层级 MUST NOT 进入 `score`，也 MUST NOT 让某个 skill 在排序里插队。
7. **信任层级不由 skill 自述**：层级只由加载位置与业务的显式指定决定，MUST NOT 读取 SKILL.md 里的字段。

## 数据契约

### `SkillFitness`（聚合，`skill/fitness.py`）

| 字段 | 含义 |
| --- | --- |
| `skill_id` | skill 标识 |
| `successes` / `failures` / `abandoned` | 三态计数 |
| `last_ts_unix` | 最近一条战绩的时间戳 |
| `cost_tokens_total` / `cost_duration_ms_total` / `cost_iterations_total` | 成本累计 |
| `discovered_selections` | `selection_origin == "discovered"` 的次数 |
| `total`（属性） | 三态之和 |
| `decided`（属性） | `successes + failures` |

### 存储协议

- `SkillFitnessStore`：`record(record)`（按 `call_id` 幂等）/ `fitness(skill_id)`。
- `SkillFitnessCatalog`：`all_fitness() -> Sequence[SkillFitness]`，顺序不作约定。
- `SkillFitnessLedger`：以上两者的合取；影子评估要求存储满足它。
- `InMemorySkillFitnessStore` 三者都实现；`all_fitness` 按 `skill_id` 排序。

### `SkillFitnessScore`

| 字段 | 含义 |
| --- | --- |
| `score` | 综合分，[0, 1]；排序与提拔阈值都用它 |
| `success_rate` | `successes / decided`；无成败样本为 0 |
| `success_lower_bound` | 成功率置信下界（未计成本） |
| `decided_samples` / `total_samples` | 成败样本数 / 全部终态执行次数 |
| `mean_cost_tokens` | `cost_tokens_total / total` |

### `FitnessScorer`（Protocol）与 `WilsonFitnessScorer`

`score(fitness) -> SkillFitnessScore`。默认实现：

- `success_lower_bound` = 成功率的 Wilson score 区间下界，分位数 `z`（默认 1.96）；
- `cost_scale_tokens=None`（默认）：`score = success_lower_bound`；
- 配置 `cost_scale_tokens` 后：`score = success_lower_bound / (1 + mean_cost_tokens / cost_scale_tokens)`；
- `z <= 0` 或 `cost_scale_tokens <= 0` 构造期 `ValueError`。

### `WorkingSetPolicy`

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `budget` | 必填 | 工作集最多容纳的 skill 数；0 = 不提拔 |
| `promote_min_score` | 0.6 | 提拔所需最低分 |
| `promote_min_samples` | 5 | 提拔所需最少成败样本 |
| `quarantine_min_samples` | 5 | 判隔离所需最少成败样本 |
| `quarantine_max_success_rate` | 0.2 | 成功率不高于该值即隔离 |
| `tier_rules` | `{}` | 来源信任层级 → `TierRule`；没有列出的层级与层级未知的 skill 用通用值 |

取值越界构造期 `ValueError`。

### `TierRule`

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `promotable` | `True` | 该层级的 skill 能否被提拔 |
| `promote_min_samples` | `None` | 该层级提拔所需最少成败样本；`None` = 沿用通用值 |
| `quarantine_min_samples` | `None` | 该层级判隔离所需最少成败样本；`None` = 沿用通用值 |
| `quarantine_exempt` | `False` | 该层级的 skill 不被隔离 |

### 来源信任分层（`skill/trust.py`）

`TrustTier = Literal["trusted", "standard", "untrusted"]`。

| 符号 | 契约 |
| --- | --- |
| `SkillTrustPolicy`（Protocol） | `tier(skill) -> TrustTier`；无副作用、同一 skill 恒得同一结果 |
| `SourceTrustPolicy(by_source=..., overrides={})` | 按 `SkillDefinition.source` 分层，`overrides`（skill id → 层级）优先。缺省 `system`→`trusted`、`user`→`standard`、`marketplace`→`untrusted`。层级取值非法或 `by_source` 没有覆盖全部来源 → 构造期 `ValueError` |
| `tier_of(policy, skill)` | 未配置信任策略时为 `None`（层级未知） |

`SkillDefinition.source` 由加载目录决定：`FilesystemSkillRegistry(skills_dirs, sources={目录: 来源})`
（`load(...)` 同参）。没有列出的目录为 `user`；声明了未加载的目录或非法来源 → `ValueError`。

### `WorkingSetView` / `WorkingSetChange`

| 类型 | 字段 |
| --- | --- |
| `WorkingSetView` | `promoted`（按分从高到低）、`hidden`（对模型隐藏）、`blocked`（拒绝派发） |
| `WorkingSetChange` | `kind`（`promoted` / `evicted` / `quarantined` / `released`）、`skill_id`、`score`（已无战绩记录为 `None`）、`trust_tier`、`trigger_call_id`（启动时重算产生的变更为 `None`） |

### `WorkingSetPlan`

`promoted`（目标工作集，按分从高到低）/ `quarantined`（目标隔离集）/ `promote` / `evict` / `quarantine` /
`release`（相对当前状态的变更）/ `changed`（属性）。

## Requirements

### Requirement: 工作集规划是无状态重算

`plan_working_set(scores, *, promoted, policy, quarantined=frozenset(), tiers=None)` 的目标状态 SHALL 只由
`scores`、`policy` 与 `tiers` 决定；`promoted` / `quarantined` 只用于算出变更。`tiers`（skill id → 来源信任层级）
给出时，每个 skill 的提拔与隔离门槛按所在层级的 `TierRule` 调整。

- 隔离条件：`decided_samples >= quarantine_min_samples` 且 `success_rate <= quarantine_max_success_rate`；
- 提拔候选：未被隔离、`decided_samples >= promote_min_samples`、`score >= promote_min_score`；
- 候选按 `score` 降序、`decided_samples` 降序、`skill_id` 升序排序，取前 `budget` 个；
- `scores` 中同一 skill 出现多次 → `ValueError`。

#### Scenario: 超预算逐出最低分者
- **GIVEN** `budget=2`，当前工作集 `{old, mid}`，三者分数 `old=0.55`、`mid=0.7`、`new=0.95`
- **WHEN** 规划
- **THEN** `promoted == ("new", "mid")`、`promote == ("new",)`、`evict == ("old",)`

#### Scenario: 高选中、低成功被隔离
- **GIVEN** skill `fake` 有 20 个成败样本、成功率 0.1，且当前在工作集内
- **WHEN** 规划
- **THEN** `quarantine == ("fake",)`、`evict == ("fake",)`，`fake` 不出现在 `promoted`

#### Scenario: 战绩好转或被重置后解除隔离
- **GIVEN** 隔离集 `{fixed, gone}`，`fixed` 当前成功率 0.9，`gone` 已无战绩记录
- **WHEN** 规划
- **THEN** `release == ("fixed", "gone")`

### Requirement: 影子评估只记录结论

`SkillFitnessShadow(store, *, policy, scorer=WilsonFitnessScorer(), observer=None)` 是 `TelemetrySink`：

- 只处理 `skill_outcome_recorded`，其余事件忽略；
- 每条战绩：计入存储 → 对 `all_fitness()` 全量算分 → `plan_working_set` → 更新自身持有的假想工作集 /
  隔离集 → 产出 `ShadowEvaluation{skill_id, call_id, score, plan, shadow=True}`；
- 结论写 INFO 日志；`observer` 非空时调用 `observer.on_evaluation(evaluation)`；
- 同一执行重复投递（存储计数未变）SHALL NOT 重复评估；
- 存储与 observer 的异常原样上抛；
- `store` 不满足 `SkillFitnessLedger` → 构造期 `TypeError`；
- `attach(engine)` 订阅全量事件流（与 `SkillFitnessRecorder.attach` 同形）。

假想状态只在内存：进程重启后由存储重算得到同样的目标状态，首轮评估把它们报告为新变更。

#### Scenario: 挂不挂影子评估，模型看到的请求相同
- **GIVEN** 同一脚本在两个 pool 上各跑一次，其中一个 engine 挂了 `SkillFitnessShadow`
- **THEN** 两次运行发给模型的每个请求（system 文本、消息序列、工具集）逐项相同

#### Scenario: 选择置信度不影响分数
- **GIVEN** 两组战绩只有 `selection_confidence` 不同（0.99 与 0.01）
- **THEN** 两组的 `score` 与 `plan` 相等

### Requirement: 生效的工作集

`SkillWorkingSet(store, policy, scorer=WilsonFitnessScorer(), quarantine_effect="hide")` 经
`DispatchPolicy(working_set=..., trust=...)` 注入。同一个实例可被一个 pool 里的全部会话共用，写入串行化。

| 方法 | 契约 |
| --- | --- |
| `observe(record)` | 计入一条战绩并重算，返回生效的变更；同一执行重复投递不重算。记录里的 `trust_tier` 用于之后的规划 |
| `restore(snapshot, trust)` | 由存储里已有的战绩重算一次；已重算过返回空。各 skill 的层级取自 `snapshot` 与 `trust` |
| `view()` | 当前结论的快照 |
| `blocks(skill_id)` | 该 skill 此刻是否被拒绝派发 |

`quarantine_effect`：

| 取值 | `hidden` | `blocked` |
| --- | --- | --- |
| `flag` | 空 | 空（只打事件） |
| `hide`（默认） | 隔离集 | 空 |
| `block` | 隔离集 | 隔离集 |

取值非法 → 构造期 `ValueError`；`store` 不满足 `SkillFitnessLedger` → 构造期 `TypeError`。

内核的读写点：

| 位置 | 行为 |
| --- | --- |
| 子 skill 到达终态 | 战绩记录填 `trust_tier` → 落盘 → `skill_outcome_recorded` → `observe` → 变更逐条打事件 |
| turn 的首次采样前 | `restore`（只在首次生效）→ 变更打事件 → `view()` 取快照，整 turn 使用这一份 |
| system prompt | `hidden` 里的 child 不进列表也不计数；deferred 模式下 `promoted` 里的 child 直接列出（按分从高到低），其余仍靠 `search_skills`；inline 模式本来就全列，不因提拔而改变 |
| `search_skills` | `hidden` 里的 skill 不进召回池（白名单内与白名单外可发现的都是）；配置了信任策略时每个候选带 `trust_tier` |
| `read_skill` / 白名单外授权 | `hidden` 里的 skill 不在可发现范围内 |
| `call_skill` | `blocks(target)` 为真 → `dispatch_rejected: skill_quarantined`，`data.reason = "skill_quarantined"` |

只配 `trust`、不配 `working_set` 时，战绩记录与召回候选带层级，其余行为不变。

状态不落盘：工作集与隔离集只由存储里的战绩与策略决定，进程重启后 `restore` 得到同样的结果。

#### Scenario: 提拔从下一轮起可见
- **GIVEN** deferred 模式的入口，`promote_min_samples=1`，工作集为空
- **WHEN** 本轮派发 `alpha` 成功
- **THEN** 本轮 emit `skill_promoted(alpha)`，但本轮的每次采样 system prompt 里都没有直接列出的 child
- **AND** 下一轮的 system prompt 直接列出 `alpha`

#### Scenario: 被隔离的 skill 对模型隐藏
- **GIVEN** `bad` 已满足隔离条件，`quarantine_effect="hide"`
- **THEN** 召回池、child 列表、提示里的 child 计数都不含 `bad`
- **AND** 按 id 直接 `call_skill(bad)` 仍会执行

#### Scenario: 拒绝派发被隔离的 skill
- **GIVEN** 同上，`quarantine_effect="block"`
- **WHEN** `call_skill(bad)`
- **THEN** 返回 `dispatch_rejected: skill_quarantined`，不启动子 turn，不产生战绩

### 事件

| kind | 时机 |
| --- | --- |
| `skill_promoted` | 进入工作集 |
| `skill_evicted` | 离开工作集（超预算被挤掉、掉线、被隔离） |
| `skill_quarantined` | 被隔离 |
| `skill_released` | 解除隔离 |

`data` 同形：`skill_id`、`score`、`success_rate`、`decided_samples`、`trust_tier`、`trigger_call_id`。
一次重算里的事件顺序：隔离、解除、逐出、提拔。都不进 LLM 视图。

## R1–R5 影响

- R1：阈值、预算、成本尺度、存储后端、信任分层口径全部由业务注入；无业务概念。
- R2：影子模式不触碰 prompt 与压缩。生效模式下 system prompt 随工作集变化，但只在 turn 之间变化
  （快照在 turn 开始时取定）；变化由战绩驱动，频率受 `promote_min_samples` 与预算约束。不触发压缩。
- R3：影子结论经 `ShadowObserver` 与 INFO 日志透出；生效的变更经四个事件透出。
- R4：纯计算 + 存储调用；随所在 turn 取消。
- R5：聚合的持久化由业务存储承担；工作集状态可由聚合重算，不落新的持久化状态。

## 显式边界

| 不做 | 说明 |
| --- | --- |
| 持久化的存储实现 | ADR 0017 规则③：内核只定协议 |
| 分离派发（`spawn_skill`）与声明式编排的战绩 | 只有 `call_skill` 子 skill 产生战绩，工作集只据此重算；被隔离的 skill 经这两条路径仍可派发 |
| 审计模式 | 工作集的结论不在 Journal 里却会改变 prompt；注入时构造期拒绝（`audit_skill_working_set_unsupported`）。只配 `trust` 不受影响 |
| 时间衰减 | 战绩不随时间失效；需要时由业务实现 `FitnessScorer` 或重置存储 |
