---
name: regex-guide
description: 正则安全指南（被 read_skill 按需读取）
version: 1.0.0
type: atomic
---
# 正则安全指南

防 ReDoS（正则灾难性回溯）：

1. 避免嵌套量词（如 `(a+)+`）与可重叠的交替。
2. 对不可信输入设输入长度上限 + 匹配超时。
3. 优先用线性时间引擎（RE2）或非回溯写法。
