---
name: summary
displayName: 入职信息小结
description: 根据已采集的问卷信息，输出一份结构化入职信息小结（纯文本，不调用工具）
version: 1.0.0
type: atomic
---

# 入职信息小结

根据上文 `questionnaire` 子 skill 采集到的问卷信息，输出一份**结构化入职信息小结**。

要求：
- 分项呈现：**岗位 / 入职日期 / 设备需求 / 待办事项**。
- 简洁、专业、客观；不臆造未采集的信息。
- 全程使用中文。
