---
name: observation-root
description: 双路观测协调器，同一个 worker 以两个独立 label 并行取证
version: 1.0.0
type: composite
entry: true
child_skills: [observation-worker]
max_call_depth: 3
---
# 双路观测协调器 EXECUTION_ROOT_MARK

第一条响应必须在同一个响应中发出两次 call_skill，均调用 observation-worker。
两次 args 分别为 {"label":"left"} 与 {"label":"right"}。这两个任务相互独立，
必须同时派发，因为工具会等待两项任务都进入后才放行；不要先等待单个任务结束。
拿到两个结果后，原样汇总 OBSERVED[left] 与 OBSERVED[right]，不要再次派发。
