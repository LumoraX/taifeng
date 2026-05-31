#!/bin/sh
# prd_check.sh —— PRD 完整性 / 落地复杂度扫描（演示用，不是真正的 PRD audit）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = proposal  （PRD 全文）
#
# 算法：扫 PRD 里是否提到 5 个标准章节（背景 / 目标 / 范围 / 非目标 / 验收）；
# 命中越多完整性越高。复杂度信号（实时 / 多端 / 并发 / 跨域）越多复杂度越高。
#
# 输出 JSON：{"completeness_score":..., "complexity_level":..., "missing_sections":...,
#             "top_issues":..., "total_score":...}

set -e

PROPOSAL="${1:-}"
if [ -z "$PROPOSAL" ]; then
    echo '{"error":"proposal is required"}' >&2
    exit 2
fi

printf '%s' "$PROPOSAL" | awk '
{
    full = full $0 " "
}
END {
    # 完整性：5 个标准章节
    n = split("背景 目标 范围 非目标 验收", sect, " ")
    hit_count = 0
    missing_str = ""
    for (i = 1; i <= n; i++) {
        if (index(full, sect[i]) > 0) {
            hit_count += 1
        } else {
            if (missing_str != "") missing_str = missing_str ","
            missing_str = missing_str "\"" sect[i] "\""
        }
    }
    completeness = 3 + hit_count * 1   # 命中数 → 3~8
    if (completeness > 10) completeness = 10

    # 复杂度：实现层信号
    cn = split("实时 多端 并发 跨域 海量", csig, " ")
    cx = 0
    for (i = 1; i <= cn; i++) {
        if (index(full, csig[i]) > 0) cx += 1
    }
    level = "low"
    if (cx >= 2) level = "medium"
    if (cx >= 4) level = "high"

    # top_issues：缺失章节 + 高复杂度提醒
    issues_str = ""
    for (i = 1; i <= n; i++) {
        if (index(full, sect[i]) == 0) {
            if (issues_str != "") issues_str = issues_str ","
            issues_str = issues_str "\"PRD 缺少「" sect[i] "」章节\""
        }
    }
    if (level == "high") {
        if (issues_str != "") issues_str = issues_str ","
        issues_str = issues_str "\"实现复杂度较高，建议拆分迭代\""
    }

    printf "{\"completeness_score\":%d,", completeness
    printf "\"complexity_level\":\"%s\",", level
    printf "\"missing_sections\":[%s],", missing_str
    printf "\"top_issues\":[%s],", issues_str
    printf "\"total_score\":%d}\n", completeness * 10
}'
