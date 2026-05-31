---
name: trip-planner
description: 行程编排器 —— 声明式 orchestration 驱动多线路并发 + 按需天气 + 汇总
version: 1.0.0
type: composite
entry: true
child_skills: [route-north, route-south, weather-probe, weather-detail, itinerary-summarizer]
tool_names: []
max_call_depth: 3
orchestration:
  steps:
    - parallel: [route-north, route-south]   # ① 两条线路并发规划（互不依赖）
    - serial: [weather-probe]                # ② 探测是否需要天气（产出布尔 flag）
    - when:                                  # ③ 条件分支：仅当上一步判定需要时才查天气
        condition: needs_weather
        then: [weather-detail]
    - serial: [itinerary-summarizer]         # ④ 汇总前序所有输出为最终行程
---

# 行程编排器（trip-planner）

本 skill 用声明式 `orchestration` 编排子 skill：两条线路并发规划 → 探测天气需求 →
（按需）查天气 → 汇总。

> 注意：声明了 `orchestration` 的 entry skill 走「纯编排器」路径——引擎按 `steps`
> 确定性驱动子 skill，**本 skill 自身不采样 LLM**；这段正文仅作人读文档。
