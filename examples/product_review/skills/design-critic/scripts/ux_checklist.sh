#!/bin/sh
# ux_checklist.sh —— 体验维度静态扫描（演示用，不是真正的 UX audit）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = prd  （PRD 全文）
#
# 算法：扫 PRD 里是否提到一组关键体验状态（空状态 / 加载态 / 错误态 / 无障碍 /
# 国际化 / 暗黑模式）；命中越多得分越高（每命中一项 + 1.5）；命中 ≤ 1 → severity=high。
#
# 输出 JSON：{"score":..., "severity":..., "top_issues":..., "hits":..., "total_score":...}

set -e

PRD="${1:-}"
if [ -z "$PRD" ]; then
    echo '{"error":"prd is required"}' >&2
    exit 2
fi

# awk 在单一 BEGIN 块里完成关键词扫描 + 评分聚合 + JSON 输出
printf '%s' "$PRD" | awk '
{
    full = full $0 " "
}
END {
    # 6 个体验关键词（命中即记一分）
    n = split("空状态 加载态 错误态 无障碍 国际化 暗黑", kws, " ")
    hits_count = 0
    hits_str = ""
    miss_str = ""
    for (i = 1; i <= n; i++) {
        if (index(full, kws[i]) > 0) {
            hits_count += 1
            if (hits_str != "") hits_str = hits_str ","
            hits_str = hits_str "\"" kws[i] "\""
        } else {
            if (miss_str != "") miss_str = miss_str ","
            miss_str = miss_str "\"未描述" kws[i] "\""
        }
    }
    score = 4 + hits_count * 1   # 命中数 → 4~10
    if (score > 10) score = 10
    severity = "low"
    if (hits_count <= 3) severity = "medium"
    if (hits_count <= 1) severity = "high"

    printf "{\"score\":%d,\"severity\":\"%s\",", score, severity
    printf "\"top_issues\":[%s],", miss_str
    printf "\"hits\":[%s],", hits_str
    printf "\"total_score\":%d}\n", score * 10
}'
