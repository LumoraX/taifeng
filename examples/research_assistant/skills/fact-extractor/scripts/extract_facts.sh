#!/bin/sh
# extract_facts.sh —— 把 source candidates JSON 转成 facts JSON（mock 提炼）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = sources_json  （上一步 source-collector 返回的 candidates 数组 JSON 字符串）
#
# Mock 行为：忽略 sources_json 内容（demo 用），直接产出 5 条预制 facts。
# 真实场景会解析 JSON + 抽取 + 去重 + 置信度评估。
#
# 输出 JSON：{"facts": [...], "n_facts": 5, "total_score": 90}

set -e

# 接收但不解析 sources_json（mock）；保留参数检验入口
_SOURCES="${1:-}"
if [ -z "$_SOURCES" ]; then
    echo '{"error":"sources_json is required"}' >&2
    exit 2
fi

cat <<'JSON'
{"facts":[{"id":1,"claim":"全球市场规模 2024 年达 X 亿美元，CAGR 18%","source_idx":0,"confidence":"high"},{"id":2,"claim":"国内龙头集中度 CR5=67%，技术路线分化明显","source_idx":1,"confidence":"high"},{"id":3,"claim":"国家专项扶持资金 200 亿元，重点支持核心技术攻关","source_idx":2,"confidence":"high"},{"id":4,"claim":"海外 3 种主流商业模式各自优劣，订阅制占比上升","source_idx":3,"confidence":"medium"},{"id":5,"claim":"一线创业者反馈核心瓶颈是人才与数据而非资金","source_idx":4,"confidence":"medium"}],"n_facts":5,"total_score":90}
JSON
