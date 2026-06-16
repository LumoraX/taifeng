# skill_outcome_fleet —— 海量多领域 skill 舰队 + 战绩沉淀

一个**大规模 skill** 体验 demo：当一个 agent 拥有几十上百个 skill 时，taifeng 怎么组织、派发，
以及「认知回路 ⑦ 沉淀」如何为每次 skill 执行落下**战绩记录**（success / failure / abandoned +
成本 + 信号来源），为后续的「按战绩提拔/逐出/防假 skill」打地基。

纯 `SimClient`，**无需 API key**。

```bash
# 1) 生成舰队（分领域单独写，25 个 SKILL.md）—— 纯 stdlib
python examples/skill_outcome_fleet/build_fleet.py

# 2) 跑 demo（3 轮，每轮 5 个域 entry）
PYTHONPATH=src python examples/skill_outcome_fleet/demo.py
```

## 舰队结构（分领域单独写 + 多目录加载）

5 个领域，每个是一个**自包含 mini-fleet**：1 个 composite 域编排器（entry）+ 4 个 atomic 叶子。

```
skills/
  data/       data-orchestrator[entry] + data-{parse,validate,transform,aggregate}
  research/   research-orchestrator[entry] + research-{search,summarize,factcheck,cite}
  devops/     devops-orchestrator[entry] + devops-{logscan,healthprobe,deploycheck,rollback}
  content/    content-orchestrator[entry] + content-{draft,edit,translate,tone}
  analysis/   analysis-orchestrator[entry] + analysis-{classify,sentiment,score,extract}
```

= **25 个 skill**（5 composite + 20 atomic）。demo 用**多目录加载**
（`EnginePool.create(skills_dir=[5 个领域 root])`）把 5 个独立 root 合并。

> **为何每域自包含**：taifeng loader 按目录各自校验 `child_skills` 引用，跨目录引用会被判
> 「未知子 skill」。故域编排器是本域 entry、只派发本域叶子。每个 `SKILL.md` body 内嵌唯一标记
> `<<ROUTE:{id}>>`，demo 据此**从 skill 图自动生成** SimClient 路由（composite → fan-out
> call_skill 子 + 汇总；atomic → 终态文本），无需为 25 个 skill 手写脚本。

## 跑了哪几轮

| 轮 | 条件 | 看什么 |
| --- | --- | --- |
| **R1 全绿** | 默认 | 全舰队成功派发，20 条 `success` 战绩 |
| **R2 注入故障叶** | 丢掉 `devops-rollback` 的路由 | 该叶被派发时 `KeyError` → `end_reason=error` → `StructuralOutcomeJudge` 判 `failure`；其余照常 |
| **R3 业务判官** | 注入自定义 `OutcomeJudge` | 战绩 `signal_source=business`——演示 R1 业务注入缝（真实业务可回调权限/校验 API） |

跨 3 轮聚合，打印「按 skill / 按领域 / 总览」三张战绩表 + v1 不变量自检。

## 输出要点

- **战绩三表**：每个叶子的 runs / ✓✗⏸ / 成本（tokens、iterations）/ 信号来源。
- **总览**：60 条记录（3 轮 × 20 叶），`success=59 / failure=1`，`structural=40 / business=20`。
- **v1 不变量自检**：`selection_origin` 恒 `whitelist`、`selection_confidence` 恒 `None`——
  **长相（置信度）与战绩（outcome）分字段存、长相绝不参与决策**（防「描述写得全但实际是假」的 skill 的根）。

## 关于 abandoned 战绩态

本 demo 展示 `success` / `failure` 两态。第三态 `abandoned` 由 `StructuralOutcomeJudge` 在
`end_reason ∈ {cancelled, max_iterations, resource_limit_exceeded, denial_circuit_open,
doom_loop_circuit_open}` 时判出——即**叶子在执行中被取消 / 触顶 / 断路器中断**。纯 SimClient 下
难以确定性地把这些落到单个叶子上（资源上限通常落在根编排器、而根不记战绩），故未在 demo 演示；
其映射在 `tests/skill/test_outcome.py` 有完整单测覆盖。

## 相关

- 能力契约：[`docs/architecture/capabilities/skill-outcome-record.md`](../../docs/architecture/capabilities/skill-outcome-record.md)
- 事件：`skill_outcome_recorded`（console 渲染为 `skil ▦ <id> outcome=… via=… cost(…)`）
- 设计背景：「工具认知回路」v1（战绩沉淀地基）
