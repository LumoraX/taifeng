---
name: 分析编排器
description: 分析领域入口：并发派发本域 4 个原子技能并汇总
version: 1.0.0
type: composite
entry: true
child_skills: [analysis-classify, analysis-sentiment, analysis-score, analysis-extract]
max_call_depth: 6
model: mock-model
---
# 分析编排器

分析领域入口：并发派发本域 4 个原子技能并汇总

> 这是「技能舰队」demo 的领域编排器（entry composite）。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> ['analysis-classify', 'analysis-sentiment', 'analysis-score', 'analysis-extract']

<<ROUTE:analysis-orchestrator>>
