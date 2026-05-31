#!/bin/sh
# lookup_segment.sh —— 输入规则编号（R1..R10）输出全名
#
# Usage: lookup_segment.sh <rule>
# 例: lookup_segment.sh R1   →   "R1: 函数行数 ≤ 80 行"

set -e

SEG="${1:?missing rule id, expected R1..R10}"

case "$SEG" in
    R1)  echo "R1: 函数行数 ≤ 80 行" ;;
    R2)  echo "R2: 圈复杂度 ≤ 10" ;;
    R3)  echo "R3: 文件行数 ≤ 800 行" ;;
    R4)  echo "R4: 禁止魔法值（用 enum / const）" ;;
    R5)  echo "R5: 禁止 silent fallback（如 except: pass）" ;;
    R6)  echo "R6: 命名一致（Python snake_case / TS camelCase）" ;;
    R7)  echo "R7: 业务异常抛自定义类型，全局拦截器统一返回" ;;
    R8)  echo "R8: SQL / Shell 注入参数化，禁止字符串拼接" ;;
    R9)  echo "R9: 注释覆盖 why 而非 what" ;;
    R10) echo "R10: 测试覆盖：边界 / 空值 / 并发 / 权限" ;;
    *)
        echo "unknown rule: $SEG (expected R1..R10)" >&2
        exit 1
        ;;
esac
