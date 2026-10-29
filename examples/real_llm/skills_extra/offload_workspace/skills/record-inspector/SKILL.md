---
name: record-inspector
description: 通过工作区分页回读超大观测记录中的指定信息
version: 1.0.0
type: composite
entry: true
child_skills: []
tool_names: [fetch_large_record, file_read]
max_call_depth: 1
---
# 观测记录检查助手

用户要求查询完整观测记录时，先调用 `fetch_large_record` 取得记录。
若工具结果已经压缩为 offload stub，全文仍在 stub 指向的工作区文件里。
头尾预览不包含中段口令，第一行会说明口令所在的零基行号。
必须调用 `file_read`，使用 stub 的相对路径以及 `offset` / `limit` 按行读取一小页，
再根据工具实际回传的中段内容回答。不要猜测口令，也不要一次读取全文。
