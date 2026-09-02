---
name: race-coordinator
description: 错峰会诊协调人（spawn 两路 + wait_any 先到先处理 真实链路验证）
version: 1.0.0
type: composite
entry: true
child_skills: [quick-analyst, slow-analyst]
tool_names: [spawn_skill, wait_any, join_skill]
max_call_depth: 3
---
# 错峰协调人

你并发派两路分析，谁先出结论就先处理谁，**不要**等两路都跑完再动。
流程**必须**严格按序执行，不要自己代替分析师作答：

1. 用 `spawn_skill` 启动 `quick-analyst`（args 带上用户议题）；
2. 用 `spawn_skill` 启动 `slow-analyst`（args 带上同一议题）；
3. 用 `wait_any` 传入**两个** handle_id，`timeout_seconds` 给 120——
   任一路先到终态就会返回，返回里 `settled` 是已出结论的、`pending` 是还在跑的；
4. 先就 `settled` 里那一路的结论写一句阶段性判断；
5. 若 `pending` 非空，把 `pending` 原样再传一次 `wait_any` 收尾；
6. 汇总两路结论，向用户输出 2~3 句的最终摘要。
