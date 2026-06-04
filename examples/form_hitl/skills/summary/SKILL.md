---
name: summary
displayName: 首诊小结
description: 根据已采集的问卷信息，输出一份结构化首诊小结（纯文本，不调用工具）
version: 1.0.0
type: atomic
---

# 首诊小结

根据上文 `questionnaire` 子 skill 采集到的问卷信息，输出一份**结构化首诊小结**。

要求：
- 分项呈现：**主诉 / 吸烟史 / 近一月症状 / 初步关注点**。
- 简洁、专业、客观；不臆造未采集的信息。
- 全程使用中文。
