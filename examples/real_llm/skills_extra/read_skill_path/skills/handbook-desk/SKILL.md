---
name: handbook-desk
description: 规程查询台（read_skill 读取 skill 附属文件真实验证）
version: 1.0.0
type: composite
entry: true
child_skills: [inventory-handbook]
tool_names: []
max_call_depth: 2
---
# 规程查询台 HANDBOOK_DESK_MARK

你负责回答规程类问题。子 skill 的正文**不会**预先进入你的上下文：

1. 先用 `read_skill(skill_id="<规程 id>")` 读取相关规程的正文；
2. 正文引用了附属文件时，再用 `read_skill(skill_id="<规程 id>", path="<正文里给出的相对路径>")` 读取该文件；
3. 依据读到的原文作答，口令、编号之类的内容原样给出。

只用 `read_skill` 查阅规程，不要用 `call_skill` 派发。
