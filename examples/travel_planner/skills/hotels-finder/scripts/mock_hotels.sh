#!/bin/sh
# mock_hotels.sh —— 返回 mock 酒店候选 JSON
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = city
#     $2 = checkin   YYYY-MM-DD
#     $3 = checkout  YYYY-MM-DD
#     $4 = guests    int
#
# 这是 demo mock；nights 用 GNU/BSD date 都不一定可用，用最朴素的字符串 diff
# （只取月-日做减法），demo 用足够；真实系统当然要走真实日期库。
#
# 输出 JSON：{"candidates": [...], "nights": N, "total_score": 88}

set -e

CITY="${1:-未知}"
CHECKIN="${2:-2026-06-01}"
CHECKOUT="${3:-2026-06-04}"
GUESTS="${4:-2}"

# 朴素算 nights：取入住与退房日期的"日"字段做差（≥1，≤30）
in_day=$(printf '%s' "$CHECKIN"  | awk -F- '{print $3+0}')
out_day=$(printf '%s' "$CHECKOUT" | awk -F- '{print $3+0}')
nights=$(( out_day - in_day ))
if [ "$nights" -le 0 ]; then nights=1; fi
if [ "$nights" -gt 30 ]; then nights=3; fi

awk -v city="$CITY" -v ci="$CHECKIN" -v co="$CHECKOUT" -v g="$GUESTS" -v n="$nights" 'BEGIN {
    printf "{\"query\":{\"city\":\"%s\",\"checkin\":\"%s\",\"checkout\":\"%s\",\"guests\":%d},", city, ci, co, g
    printf "\"nights\":%d,", n
    printf "\"candidates\":["
    printf "{\"name\":\"%s 中心商务酒店\",\"stars\":4,\"price_per_night\":680,\"district\":\"市中心\",\"rating\":8.7,\"amenities\":[\"wifi\",\"breakfast\",\"gym\"]},", city
    printf "{\"name\":\"%s 海景度假酒店\",\"stars\":5,\"price_per_night\":1280,\"district\":\"海滨\",\"rating\":9.2,\"amenities\":[\"wifi\",\"breakfast\",\"pool\",\"spa\"]},", city
    printf "{\"name\":\"%s 文化主题民宿\",\"stars\":3,\"price_per_night\":380,\"district\":\"老城区\",\"rating\":8.4,\"amenities\":[\"wifi\",\"kitchen\"]}", city
    printf "],\"total_score\":88}\n"
}'
