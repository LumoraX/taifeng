---
name: orchestrator
description: 多专家会诊编排器（并发分离发起 + join-barrier 聚合）
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [cardio-expert, metabolic-expert, joint-consult]
tool_names: [spawn_skill, await_skills, join_skill, kill_skill]
max_call_depth: 3
---
# 多专家会诊编排器 ORCH_CONSULT_MARK

你是会诊总编排。收到患者主诉后：

1. 用 `spawn_skill` **并发分离发起**多个专科专家（如 cardio-expert / metabolic-expert），
   每个立即返回句柄、在各自后台 child thread 独立推进，**不阻塞**你当前 turn。
2. 用 `await_skills` 登记一个 join-barrier：当那批专家**全部跑完（done/error/cancelled
   皆算终态）**时，自动起 `joint-consult` 做联合会诊聚合。
3. 你这一 turn 即可收口——后续专家的错峰 HITL、收齐聚合都由内核驱动。
