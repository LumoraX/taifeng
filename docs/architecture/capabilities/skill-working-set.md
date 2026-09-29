# Capability: skill-working-set

> 状态：Experimental（影子模式）。关联 ADR 0017（规则②）、0067、0077；
> 设计稿 `2026-06-16-skill-capability-acquisition-loop-design.md` §6 相位 5。
> 实现：`src/taifeng/skill/working_set.py`（算分 + 规划，纯函数）、`src/taifeng/skill/fitness_shadow.py`
> （影子评估）、`src/taifeng/skill/fitness.py`（聚合）。经 `taifeng.experimental` 导出。

## Purpose

认知回路 ⑦ 沉淀相位的决策部分：按**真实执行结果**给 skill 算分，规划哪些 skill 值得常驻工作记忆、哪些
「描述过度承诺」该被隔离。当前只提供**影子模式**——算分、规划、记录结论，不改变任何 skill 的可见性、
排序、召回或派发。

## 不变量

1. **长相不得喂战绩**：算分与规划 MUST NOT 读取 `selection_confidence`。聚合里的 `discovered_selections`
   只是「经发现被选中的次数」，不含置信度。
2. **没真干成过就不涨分**：没有成败样本的 skill 得 0 分。
3. **放弃不算失败**：`abandoned`（取消 / 人拒绝）不进成功率分母。
4. **影子不生效**：内核的 prompt 组装、召回、派发路径 SHALL NOT 持有影子评估组件的引用。

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

取值越界构造期 `ValueError`。

### `WorkingSetPlan`

`promoted`（目标工作集，按分从高到低）/ `quarantined`（目标隔离集）/ `promote` / `evict` / `quarantine` /
`release`（相对当前状态的变更）/ `changed`（属性）。

## Requirements

### Requirement: 工作集规划是无状态重算

`plan_working_set(scores, *, promoted, policy, quarantined=frozenset())` 的目标状态 SHALL 只由 `scores` 与 `policy`
决定；`promoted` / `quarantined` 只用于算出变更。

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

## R1–R5 影响

- R1：阈值、预算、成本尺度、存储后端全部由业务注入；无业务概念。
- R2：影子模式不触碰 prompt 与压缩。
- R3：结论经 `ShadowObserver` 与 INFO 日志透出；数据源是既有的 `skill_outcome_recorded` 事件。
- R4：纯计算 + 存储调用；随 `attach` 所在任务取消。
- R5：聚合的持久化由业务存储承担；影子状态可由聚合重算。

## 显式边界

| 不做 | 说明 |
| --- | --- |
| 让结论生效（改变可见性 / 排序 / 召回 / 派发） | 生效路径另行立项，启用前须先用影子数据核对结论 |
| skill 来源信任分层 | 随生效路径一并定义 |
| 持久化的存储实现 | ADR 0017 规则③：内核只定协议 |
