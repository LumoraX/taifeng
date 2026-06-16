---
name: 研究调研编排器
description: 研究调研领域入口：并发派发本域 4 个原子技能并汇总
version: 1.0.0
type: composite
entry: true
child_skills: [research-search, research-summarize, research-factcheck, research-cite]
max_call_depth: 6
model: mock-model
---
# 研究调研编排器

研究调研领域入口：并发派发本域 4 个原子技能并汇总

> 这是「技能舰队」demo 的领域编排器（entry composite）。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> ['research-search', 'research-summarize', 'research-factcheck', 'research-cite']

<<ROUTE:research-orchestrator>>
