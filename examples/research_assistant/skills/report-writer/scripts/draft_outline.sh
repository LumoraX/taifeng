#!/bin/sh
# draft_outline.sh —— 把 facts JSON 转成报告 outline JSON（mock 起草）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = topic
#     $2 = facts_json  （fact-extractor 返回的 facts 数组 JSON 字符串）
#
# Mock 行为：按 topic 套四章经典调研报告模板，fact_ids 用固定分组演示
# "事实 → 章节"映射关系。真实场景会按事实聚类自动分章。
#
# 输出 JSON：{"outline": {...}, "total_score": 87}

set -e

TOPIC="${1:-未指定议题}"
_FACTS="${2:-}"
if [ -z "$_FACTS" ]; then
    echo '{"error":"facts_json is required"}' >&2
    exit 2
fi

awk -v topic="$TOPIC" 'BEGIN {
    printf "{\"outline\":{"
    printf "\"title\":\"%s 调研报告\",", topic
    printf "\"sections\":["
    printf "{\"heading\":\"1. 市场背景与规模\",\"fact_ids\":[1,3]},"
    printf "{\"heading\":\"2. 竞争格局与主流玩家\",\"fact_ids\":[2,4]},"
    printf "{\"heading\":\"3. 核心瓶颈与突破方向\",\"fact_ids\":[5]},"
    printf "{\"heading\":\"4. 结论与建议\",\"fact_ids\":[]}"
    printf "]},\"total_score\":87}\n"
}'
