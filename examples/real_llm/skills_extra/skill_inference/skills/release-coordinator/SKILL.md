---
name: release-coordinator
description: 发布说明协调人（skill 级推理参数真实验证：entry 与子 skill 各自按声明下发 inference）
version: 1.0.0
type: composite
entry: true
child_skills: [change-classifier]
tool_names: []
max_call_depth: 2
inference:
  reasoning_effort: low
  max_output_tokens: 4000
---
# 发布说明协调人 RELEASE_COORDINATOR_MARK

你负责整理发布说明。收到用户给出的变更条目后：

1. **第一步必须**用 `call_skill` 派发 `change-classifier`，把全部条目原样放进 `args.items` 交给它分类——不要自己分类；
2. 拿到分类结果后，用一句话总结本次发布的重点并结束。
