---
name: 内容生产编排器
description: 内容生产领域入口：并发派发本域 4 个原子技能并汇总
version: 1.0.0
type: composite
entry: true
child_skills: [content-draft, content-edit, content-translate, content-tone]
max_call_depth: 6
model: mock-model
---
# 内容生产编排器

内容生产领域入口：并发派发本域 4 个原子技能并汇总

> 这是「技能舰队」demo 的领域编排器（entry composite）。它通过 ``call_skill`` 并发派发下列子技能，
> 每个子技能终态都会落一条 SkillExecutionRecord：
>
> ['content-draft', 'content-edit', 'content-translate', 'content-tone']

<<ROUTE:content-orchestrator>>
