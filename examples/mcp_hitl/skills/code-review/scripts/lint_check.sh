#!/bin/sh
# lint_check.sh —— 极简危险模式扫描（演示用，不是真正的 linter）
#
# Taifeng run_script 把 args 字典按 schema properties 顺序展开为 positional argv。
# 本脚本 args_schema 仅一项 ``code``，所以：
#
#     argv[1] = code  （待审查的代码原文）
#
# 输出 JSON：{"hits": [{"pattern":..., "line":N, "snippet":"..."}]}

set -e

CODE="${1:-}"
if [ -z "$CODE" ]; then
    echo "usage: lint_check.sh <code-text>" >&2
    exit 2
fi

PATTERNS="eval exec system shell=True f.SELECT subprocess.call rm -rf"

# 用 awk 一遍扫所有匹配
printf '%s\n' "$CODE" | awk -v pats="$PATTERNS" '
BEGIN {
    n = split(pats, arr, " ")
    first = 1
    printf("{\"hits\":[")
}
{
    for (i = 1; i <= n; i++) {
        if (index($0, arr[i]) > 0) {
            if (!first) printf(",")
            first = 0
            line = $0
            gsub(/"/, "\\\"", line)
            printf("{\"pattern\":\"%s\",\"line\":%d,\"snippet\":\"%s\"}",
                   arr[i], NR, line)
        }
    }
}
END {
    printf("]}\n")
}
'
