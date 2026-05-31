#!/bin/sh
# mock_flights.sh —— 返回 mock 航班候选 JSON
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = origin
#     $2 = destination
#     $3 = date
#
# 这是 demo mock，没有真实调用航空公司 API；候选数据 + 价格按目的地哈希一致
# 化产出（同 origin/destination 输入跑出同结果），方便 LLM 综合时引用稳定。
#
# 输出 JSON：{"candidates": [...], "total_score": 85, "query": {...}}

set -e

ORIGIN="${1:-未知}"
DEST="${2:-未知}"
DATE="${3:-2026-06-01}"

# 用 awk 一次性输出整个 JSON（避免多 echo 拼接时空格转义问题）
awk -v origin="$ORIGIN" -v dest="$DEST" -v date="$DATE" 'BEGIN {
    printf "{\"query\":{\"origin\":\"%s\",\"destination\":\"%s\",\"date\":\"%s\"},", origin, dest, date
    printf "\"candidates\":["
    printf "{\"flight_no\":\"CA1801\",\"depart\":\"08:00\",\"arrive\":\"10:30\",\"duration_min\":150,\"price\":1280,\"airline\":\"国航\"},"
    printf "{\"flight_no\":\"MU5102\",\"depart\":\"13:15\",\"arrive\":\"15:50\",\"duration_min\":155,\"price\":1080,\"airline\":\"东航\"},"
    printf "{\"flight_no\":\"HU7891\",\"depart\":\"19:40\",\"arrive\":\"22:15\",\"duration_min\":155,\"price\":890,\"airline\":\"海航\"}"
    printf "],\"total_score\":85}\n"
}'
