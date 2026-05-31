#!/bin/sh
# complexity_estimate.sh —— 工程复杂度静态扫描（演示用）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = prd  （PRD 全文）
#
# 算法：扫 PRD 里是否提到复杂度信号（实时 / 多端 / 并发 / 事务 / 跨服务 / AI 模型）；
# 命中越多复杂度越高 → score 越低，severity 越高。同时给个工程日估算。
#
# 输出 JSON：{"score":..., "severity":..., "top_issues":..., "complexity_signals":...,
#             "estimated_eng_days":..., "total_score":...}

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
    n = split("实时 多端 并发 事务 跨服务 AI 模型 海量", kws, " ")
    sig_count = 0
    sig_str = ""
    issues_str = ""
    for (i = 1; i <= n; i++) {
        if (index(full, kws[i]) > 0) {
            sig_count += 1
            if (sig_str != "") sig_str = sig_str ","
            sig_str = sig_str "\"" kws[i] "\""
            issue = "需澄清「" kws[i] "」实现路径"
            if (issues_str != "") issues_str = issues_str ","
            issues_str = issues_str "\"" issue "\""
        }
    }
    # 复杂度信号越多 → 分越低（10 - 信号数 × 1.5）
    score = 10 - int(sig_count * 1.5)
    if (score < 1) score = 1
    severity = "low"
    if (sig_count >= 2) severity = "medium"
    if (sig_count >= 4) severity = "high"
    # 估时基线 5 天 + 每信号 5 天
    eng_days = 5 + sig_count * 5

    printf "{\"score\":%d,\"severity\":\"%s\",", score, severity
    printf "\"top_issues\":[%s],", issues_str
    printf "\"complexity_signals\":[%s],", sig_str
    printf "\"estimated_eng_days\":%d,", eng_days
    printf "\"total_score\":%d}\n", score * 10
}'
