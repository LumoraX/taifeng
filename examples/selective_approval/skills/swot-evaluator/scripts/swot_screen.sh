#!/bin/sh
# swot_screen.sh —— SWOT 四象限信号词扫描（演示用）
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = proposal  （PRD / 方案全文）
#
# 算法：4 组关键词分别对应 SWOT 四象限。哪个象限命中信号最多 + 内外部维度
# 综合，给出战略象限建议（SO/WO/ST/WT）。
#
# 输出 JSON：{"strengths":..., "weaknesses":..., "opportunities":..., "threats":...,
#             "quadrant":..., "total_score":...}

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
    # 用四套关键词做象限识别（命中数 → 内/外、正/负）
    sn = split("领先 差异化 专利 自研 团队 经验", skws, " ")
    wn = split("缺失 不足 短板 依赖 成本高 风险", wkws, " ")
    on = split("市场 增长 政策扶持 新兴 蓝海 机会", okws, " ")
    tn = split("竞争 监管 替代 周期 衰退 不确定", tkws, " ")

    s_str = ""; s_cnt = 0
    w_str = ""; w_cnt = 0
    o_str = ""; o_cnt = 0
    t_str = ""; t_cnt = 0

    for (i = 1; i <= sn; i++) if (index(full, skws[i]) > 0) {
        s_cnt += 1
        if (s_str != "") s_str = s_str ","
        s_str = s_str "\"" skws[i] "\""
    }
    for (i = 1; i <= wn; i++) if (index(full, wkws[i]) > 0) {
        w_cnt += 1
        if (w_str != "") w_str = w_str ","
        w_str = w_str "\"" wkws[i] "\""
    }
    for (i = 1; i <= on; i++) if (index(full, okws[i]) > 0) {
        o_cnt += 1
        if (o_str != "") o_str = o_str ","
        o_str = o_str "\"" okws[i] "\""
    }
    for (i = 1; i <= tn; i++) if (index(full, tkws[i]) > 0) {
        t_cnt += 1
        if (t_str != "") t_str = t_str ","
        t_str = t_str "\"" tkws[i] "\""
    }

    # 命中均为 0 时给保底值，避免空数组让 LLM 困惑
    if (s_cnt == 0) s_str = "\"待补充内部优势\""
    if (w_cnt == 0) w_str = "\"待补充内部不足\""
    if (o_cnt == 0) o_str = "\"待补充外部机会\""
    if (t_cnt == 0) t_str = "\"待补充外部威胁\""

    # 象限：内部优势 vs 不足 + 外部机会 vs 威胁
    internal_pos = (s_cnt >= w_cnt)
    external_pos = (o_cnt >= t_cnt)
    quadrant = "WT"  # 收缩
    if (internal_pos && external_pos) quadrant = "SO"   # 进攻
    else if (!internal_pos && external_pos) quadrant = "WO"  # 改善
    else if (internal_pos && !external_pos) quadrant = "ST"  # 防御

    # 总分：内外正向信号占比 (0~100)
    total = (s_cnt + o_cnt) * 100 / ((s_cnt + w_cnt + o_cnt + t_cnt) + 0.001)
    if (total < 30) total = 30

    printf "{\"strengths\":[%s],", s_str
    printf "\"weaknesses\":[%s],", w_str
    printf "\"opportunities\":[%s],", o_str
    printf "\"threats\":[%s],", t_str
    printf "\"quadrant\":\"%s\",", quadrant
    printf "\"total_score\":%d}\n", int(total)
}'
