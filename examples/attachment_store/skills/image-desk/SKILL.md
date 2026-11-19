---
name: image-desk
description: 收下图片，之后按需重新查看已移出上下文的图片
version: 1.0.0
type: composite
entry: true
tool_names: [file_read]
max_call_depth: 1
---

# 图片台

用户发来图片时简单确认。之后问起已看不到的图片时，对话里会有一行 `[evicted: ...]`，
其中 `path=` 后面是原件位置；用 `file_read` 读这个 path 重新查看。
