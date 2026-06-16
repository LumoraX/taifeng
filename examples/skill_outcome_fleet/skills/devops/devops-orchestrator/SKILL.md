---
name: 运维编排器
description: 运维领域入口：并发派发本域 4 个原子技能并汇总
version: 1.0.0
type: composite
entry: true
child_skills: [devops-logscan, devops-healthprobe, devops-deploycheck, devops-rollback]
max_call_depth: 6
model: mock-model
---
# 运维编排器

运维领域入口：并发派发本域 4 个原子技能并汇总

> 这是「技能舰队」demo 的领域编排器（entry composite）。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> ['devops-logscan', 'devops-healthprobe', 'devops-deploycheck', 'devops-rollback']

<<ROUTE:devops-orchestrator>>
