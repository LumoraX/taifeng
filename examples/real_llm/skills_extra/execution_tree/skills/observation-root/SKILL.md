---
name: observation-root
description: 双路观测协调器，同一个 worker 以两个独立 label 并行取证
version: 1.0.0
type: composite
entry: true
child_skills: [observation-worker]
tool_names: [spawn_skill, join_skill]
max_call_depth: 3
---
# 双路观测协调器 EXECUTION_ROOT_MARK

先用 spawn_skill 启动两个 observation-worker，args 分别为 {"label":"left"} 与
{"label":"right"}。可分两次响应启动，也可同一响应发出两次；两个任务相互独立。
在两个句柄都返回之前，不要等待或查询第一个结束。
随后用 join_skill 查询两个 handle_ids；若尚未全为 done，继续查询，直到收到两个实际结果。
最后原样汇总 OBSERVED[left] 与 OBSERVED[right]，不要只报告已启动，不要猜结果。
