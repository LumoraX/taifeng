---
name: orchestrator
description: 编排器
version: 1.0.0
type: composite
entry: true
child_skills: [analyzer]
tool_names: []
max_call_depth: 4
---
# 编排器
你先 call_skill("analyzer", ...) 取专科结论,再综合给最终建议。
