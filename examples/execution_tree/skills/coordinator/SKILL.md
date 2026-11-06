---
name: coordinator
description: 并行演示同一 Skill 两次与另一项任务
version: 1.0.0
type: composite
entry: true
model: sim-model
child_skills: [worker, other]
max_call_depth: 4
---
# TREE_COORDINATOR_MARK

并行 call_skill 调用 worker 两次（不同任务参数），同时调用 other。
