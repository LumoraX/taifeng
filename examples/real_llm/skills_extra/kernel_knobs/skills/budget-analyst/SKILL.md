---
name: budget-analyst
description: 预算分析（K2 token 天花板真实触发验证）
version: 1.0.0
type: composite
entry: true
child_skills: [reference-notes]
tool_names: []
max_call_depth: 2
---
# 预算分析

你是分析助手。回答任何问题前，**必须先**用 `read_skill` 读取 `reference-notes`
获取参考口径，然后才能作答。
