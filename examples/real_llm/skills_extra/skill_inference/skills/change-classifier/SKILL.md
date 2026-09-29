---
name: change-classifier
description: 把变更条目逐条归入「修复 / 新功能 / 维护」三类
version: 1.0.0
type: atomic
inference:
  reasoning_effort: medium
  max_output_tokens: 2500
---
# 变更分类器 CHANGE_CLASSIFIER_MARK

把输入里的每条变更归入「修复 / 新功能 / 维护」之一，每行输出「条目 → 类别」，不要多余解释。
