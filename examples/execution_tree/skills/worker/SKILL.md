---
name: worker
description: 读取事实再完成工作的模拟子任务
version: 1.0.0
type: composite
child_skills: [facts]
max_call_depth: 4
---
# TREE_WORKER_MARK

使用 read_skill 读取 facts，随后返回结果。
