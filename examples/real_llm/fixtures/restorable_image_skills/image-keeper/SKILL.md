---
name: image-keeper
description: 先收下用户给的图片，之后按需重新查看已移出上下文的图片
version: 1.0.0
type: composite
entry: true
tool_names: [file_read]
max_call_depth: 1
---

# 图片保管员

当用户发来图片并说「先收着」时，只回复两个字「收到」。此时不要描述图片，不要读出图片里的任何文字或编号。

当用户之后问起某张图片的内容、而你在对话里已经看不到这张图片时：对话里会有一行以
`[evicted:` 开头的记录，其中 `path=` 后面是图片原件的保存位置。用 `file_read` 读取这个
path 重新查看图片，然后依据看到的内容回答。不要凭猜测回答。
