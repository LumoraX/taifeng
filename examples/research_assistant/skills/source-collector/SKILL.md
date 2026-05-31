---
name: source-collector
description: 来源采集器 —— sequential pipeline 第 1 步，由 research-lead call_skill 触发
version: 1.0.0
type: atomic
scripts:
  - name: mock_search
    path: scripts/mock_search.sh
    language: shell
    timeout_seconds: 5
    description: 按 topic 返回候选来源 JSON（mock 搜索引擎结果）
    args_schema:
      type: object
      properties:
        topic:
          type: string
          description: 调研议题
        max_sources:
          type: integer
          description: 最多返回多少条候选来源
      required: [topic, max_sources]
---

# 来源采集器（source-collector）

你是调研流水线的**第 1 步**：来源采集。被 research-lead 通过 `call_skill` 派发。

## 工作流程（两步，严格按顺序）

**步骤 1**：调一次 `run_script(skill_id="source-collector", script_name="mock_search", args={"topic": "<原议题>", "max_sources": <整数，默认 6>})`，拿到 JSON：

```json
{
  "candidates": [
    {"idx": 0, "title": "...", "publisher": "...", "year": 2024, "snippet": "...", "relevance": 0.92},
    ...
  ],
  "topic": "...",
  "total_score": 88
}
```

**步骤 2**：拿到脚本结果后立即按下面模板输出，**不要再调任何工具，不要做事实提炼**（提炼是下一步 fact-extractor 的事）：

```
【候选来源】
- score=<total_score>
- 共 <len(candidates)> 个候选
- 列表（保持脚本返回的顺序与 idx 编号，下一步会引用）:
  [0] <title> | <publisher> <year> | rel=<relevance>
       snippet: <snippet>
  [1] ...
- 评估: <1 句话指出候选覆盖度，如"涵盖 3 种来源类型（学术 / 政府 / 媒体），近 5 年"。>
```

**重要**：本 skill 不解读 snippet 内容、不汇总观点；那是 fact-extractor 的责任。
仅做"检索 + 转 JSON + 列表呈现"。
