#!/bin/sh
# mock_activities.sh —— 返回 mock 活动 / 景点候选 JSON
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = city
#     $2 = days       int
#     $3 = interests  逗号分隔关键词
#
# 候选清单含 8 项覆盖 4 个品类 × 3 个时段，方便 trip-planner 按 best_time 槽位
# 排进按日行程。score 是固定 92 演示用。
#
# 输出 JSON：{"candidates": [...], "city": "...", "total_score": 92}

set -e

CITY="${1:-未知}"
DAYS="${2:-3}"
INTERESTS="${3:-未指定}"

awk -v city="$CITY" -v days="$DAYS" -v ints="$INTERESTS" 'BEGIN {
    printf "{\"query\":{\"city\":\"%s\",\"days\":%d,\"interests\":\"%s\"},", city, days, ints
    printf "\"city\":\"%s\",", city
    printf "\"candidates\":["
    printf "{\"name\":\"老城区漫步\",\"category\":\"景点\",\"duration_h\":3.0,\"best_time\":\"上午\",\"cost\":0,\"rating\":9.0},"
    printf "{\"name\":\"本地市场早餐之旅\",\"category\":\"美食\",\"duration_h\":1.5,\"best_time\":\"上午\",\"cost\":80,\"rating\":9.3},"
    printf "{\"name\":\"中央博物馆\",\"category\":\"博物馆\",\"duration_h\":2.5,\"best_time\":\"下午\",\"cost\":60,\"rating\":8.8},"
    printf "{\"name\":\"近郊国家公园徒步\",\"category\":\"户外\",\"duration_h\":5.0,\"best_time\":\"上午\",\"cost\":120,\"rating\":9.5},"
    printf "{\"name\":\"米其林餐厅晚宴\",\"category\":\"美食\",\"duration_h\":2.0,\"best_time\":\"晚上\",\"cost\":680,\"rating\":9.4},"
    printf "{\"name\":\"江边夜景骑行\",\"category\":\"户外\",\"duration_h\":1.5,\"best_time\":\"晚上\",\"cost\":50,\"rating\":8.9},"
    printf "{\"name\":\"地下古城遗址\",\"category\":\"景点\",\"duration_h\":2.0,\"best_time\":\"下午\",\"cost\":90,\"rating\":8.6},"
    printf "{\"name\":\"现代艺术馆\",\"category\":\"博物馆\",\"duration_h\":1.5,\"best_time\":\"下午\",\"cost\":50,\"rating\":8.4}"
    printf "],\"total_score\":92}\n"
}'
