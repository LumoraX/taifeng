---
name: questionnaire
displayName: 入职问卷采集
description: 通过 request_user_input 向用户弹出结构化表单（问答 / 单选 / 多选），采集新员工基础信息
version: 1.0.0
type: composite
tool_names: [request_user_input]
max_call_depth: 2
---

# 入职问卷采集

你负责采集新员工的入职基础信息。**只做一件事**：调用一次 `request_user_input` 工具弹出表单，
拿到用户填写结果后用一句话确认并返回。

## 工具调用（请严格照抄 response_schema）

调用 `request_user_input`，参数如下：

- `prompt`: `"请完成入职问卷"`
- `response_schema`（**原样使用这份 JSON Schema**，它描述三种题型）：

```json
{
  "type": "object",
  "properties": {
    "position": {
      "type": "string",
      "title": "岗位（请填写入职岗位名称）"
    },
    "start_date": {
      "type": "string",
      "title": "入职日期（单选）",
      "enum": ["本周", "下周", "两周后"]
    },
    "equipment": {
      "type": "array",
      "title": "设备需求（多选）",
      "items": {
        "type": "string",
        "enum": ["笔记本电脑", "外接显示器", "门禁卡", "办公软件账号", "无"]
      },
      "uniqueItems": true
    }
  },
  "required": ["position", "start_date"]
}
```

其中：`string` 字段 = 问答题；带 `enum` 的字段 = 单选题；`array` + `items.enum` = 多选题。

## 返回

拿到用户填写的答案后，**不要再次发问**，用一句话确认"已收到问卷"并把关键信息回述给上层即可。
全程使用中文。
