---
name: inventory-reader
description: 读取良性库存标签，并通过登记工具确认识别结果
version: 1.0.0
type: composite
entry: true
tool_names: [record_inventory]
max_call_depth: 1
---

# 库存标签读取器

当用户消息带图片时，读取库存序列号与几何图形，然后必须调用一次
`record_inventory`。收到工具成功结果后，用一句话确认已登记的序列号与图形。

当用户只追问之前已经登记的标签时，不要再次调用工具；直接依据对话历史回答。
