#!/bin/sh
# test_surface.sh —— 测试覆盖面 / 上线风险静态扫描（演示用）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = prd  （PRD 全文）
#
# 算法：扫 PRD 里是否提到边界覆盖维度（边界 / 异常 / 灰度 / 回滚 / 监控 / 兼容性）；
# 命中越少风险越大。同时检测是否明确"回滚预案"。
#
# 输出 JSON：{"score":..., "severity":..., "top_issues":..., "uncovered_axes":...,
#             "rollback_ready":..., "total_score":...}

set -e

PRD="${1:-}"
if [ -z "$PRD" ]; then
    echo '{"error":"prd is required"}' >&2
    exit 2
fi

printf '%s' "$PRD" | awk '
{
    full = full $0 " "
}
END {
    n = split("边界 异常 灰度 回滚 监控 兼容", kws, " ")
    cov_count = 0
    uncovered_str = ""
    issues_str = ""
    rollback = "false"
    for (i = 1; i <= n; i++) {
        if (index(full, kws[i]) > 0) {
            cov_count += 1
            if (kws[i] == "回滚") rollback = "true"
        } else {
            if (uncovered_str != "") uncovered_str = uncovered_str ","
            uncovered_str = uncovered_str "\"" kws[i] "\""
            issue = "未定义" kws[i] "策略"
            if (issues_str != "") issues_str = issues_str ","
            issues_str = issues_str "\"" issue "\""
        }
    }
    # 覆盖维度越多 → 分越高
    score = 3 + cov_count * 1
    if (score > 10) score = 10
    severity = "low"
    if (cov_count <= 3) severity = "medium"
    if (cov_count <= 1 || rollback == "false") severity = "high"

    printf "{\"score\":%d,\"severity\":\"%s\",", score, severity
    printf "\"top_issues\":[%s],", issues_str
    printf "\"uncovered_axes\":[%s],", uncovered_str
    printf "\"rollback_ready\":%s,", rollback
    printf "\"total_score\":%d}\n", score * 10
}'
