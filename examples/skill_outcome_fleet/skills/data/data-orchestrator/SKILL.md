---
name: 数据处理编排器
description: 数据处理领域入口：并发派发本域 4 个原子技能并汇总
version: 1.0.0
type: composite
entry: true
child_skills: [data-parse, data-validate, data-transform, data-aggregate]
max_call_depth: 6
model: mock-model
---
# 数据处理编排器

数据处理领域入口：并发派发本域 4 个原子技能并汇总

> 这是「技能舰队」demo 的领域编排器（entry composite）。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> ['data-parse', 'data-validate', 'data-transform', 'data-aggregate']

<<ROUTE:data-orchestrator>>
