# turn-rewind —— 自治链「一键跑完」+ 回退到任意节点重跑

演示内核的 **turn-rewind** 能力:把一次 root turn 的执行轨迹拆成一张**可寻址的回访
节点表**,业务侧可对**任意节点直接 retry**。子 skill 全程 `entry: false`,继续被自治链
`call_skill` 一键跑完 —— **不碰 entry/call_skill 互斥,不放松任何约束**。

> 契约见 [`docs/architecture/capabilities/turn-rewind.md`](../../docs/architecture/capabilities/turn-rewind.md)，决策见 [ADR 0014](../../docs/decisions/0014-turn-rewind.md)。

## 跑

```bash
cd taifeng
PYTHONPATH=src uv run python examples/turn_rewind/demo.py   # 无需 API key（MockClient）
```

## 节点表(一次 turn 拆出的可寻址节点)

`orchestrator`(entry composite)一句话跑完:LLM 采样 → `call_skill(analyzer)` → 综合。
这一个 root turn 被拆成:

| 节点 | 是什么 | rewind 它 = |
| --- | --- | --- |
| `it1` / `it2` … | 每圈 LLM 采样前(iteration 节点) | 重采样该圈,LLM **重新决定**下游(`re_reason`) |
| `disp0` … | 每次 `call_skill` / 工具派发(dispatch 节点) | `retry_tool`:保留 assistant「决定调它」、只重跑该工具；`re_reason`:截到该圈采样前重推 |

业务侧 `engine.rewind_nodes()` 取节点、`engine.submit(Rewind(node_id, mode))` 回退重推。

## 两个场景

**A. `retry_tool` 重跑一次 `call_skill`**(头号场景:重跑自治 run 里的中间某一步)

```
[一键跑完]    → call_skill(analyzer)=风险偏高(初版) → 综合:加强监测(基于初版)
Rewind(disp0, retry_tool)
[retry_tool] → analyzer 走新子 turn=风险中等(修订版) → 综合:常规随访(基于修订版)
```
保留「LLM 决定调 analyzer」的动作,只把 analyzer 子 skill 重跑一遍、换掉它的输出,
父 skill 基于新结论续推。子 skill 是 `entry: false`,照样被 `call_skill` 重跑。

**B. `re_reason` 回退到某圈采样前**(LLM 重新决定下游)

```
[一键跑完]   → call_skill(analyzer)=风险偏高 → 综合:加强监测
Rewind(it1, re_reason)
[re_reason]  → LLM 重判:信息不足,先补检查（这次没派发 analyzer，下游自适应）
```

## 与 step_pipeline 的区别

- **turn-rewind(本例)**:留在自治 `call_skill` 链内,LLM 动态编排「一键跑完」+ 节点级重试。
- **[step_pipeline](../step_pipeline/)**:把编排下沉业务层的确定性范式(每步 `entry: true` 单独 session)。

两者按需选用:要「LLM 自主一键跑完 + 重跑中间步」→ turn-rewind;要「业务确定性编排 + 步级重放」→ step_pipeline。

## R 红线

R2 rewind 蓄意回退 cache anchor → 首采样失效标 expected（不计 unexpected_breaks）；
R5 截断仅内存、store JSONL append-only + rewind marker 留痕；R3 `rewind_checkpoint_recorded` /
`turn_rewound` / `rewind_rejected` 三事件可观测。
