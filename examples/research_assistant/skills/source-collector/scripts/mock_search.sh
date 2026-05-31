#!/bin/sh
# mock_search.sh —— 返回 mock 调研来源候选 JSON
#
# Taifeng run_script 把 args 按 schema properties 顺序展开为 positional argv：
#     $1 = topic
#     $2 = max_sources  int
#
# 输出 6 条固定的多类型来源（学术 / 政府 / 媒体 / 行业报告），让 LLM 体会"广撒网"语义。
#
# 输出 JSON：{"candidates": [...], "topic": "...", "total_score": 88}

set -e

TOPIC="${1:-未指定}"
MAX="${2:-6}"

awk -v topic="$TOPIC" -v max="$MAX" 'BEGIN {
    printf "{\"topic\":\"%s\",", topic
    printf "\"candidates\":["
    printf "{\"idx\":0,\"title\":\"%s 行业 2024 白皮书\",\"publisher\":\"中信证券研究所\",\"year\":2024,\"snippet\":\"全球市场规模 2024 年达 X 亿美元，CAGR 18%%\",\"relevance\":0.92},", topic
    printf "{\"idx\":1,\"title\":\"%s 现状分析与展望\",\"publisher\":\"清华大学产业研究中心\",\"year\":2024,\"snippet\":\"国内龙头集中度 CR5=67%%，技术路线分化明显\",\"relevance\":0.89},", topic
    printf "{\"idx\":2,\"title\":\"%s 相关政策汇编\",\"publisher\":\"国家发改委\",\"year\":2024,\"snippet\":\"专项扶持资金 200 亿元，重点支持核心技术攻关\",\"relevance\":0.86},", topic
    printf "{\"idx\":3,\"title\":\"海外 %s 商业模式调研\",\"publisher\":\"麦肯锡全球研究院\",\"year\":2023,\"snippet\":\"3 种主流商业模式各自优劣，订阅制占比上升\",\"relevance\":0.83},", topic
    printf "{\"idx\":4,\"title\":\"%s 一线访谈实录\",\"publisher\":\"36 氪深度\",\"year\":2025,\"snippet\":\"创业者反馈：核心瓶颈是人才与数据，而非资金\",\"relevance\":0.79},", topic
    printf "{\"idx\":5,\"title\":\"%s 技术演进路径\",\"publisher\":\"ACM Computing Surveys\",\"year\":2023,\"snippet\":\"算法层近 5 年三次范式跃迁，工程化仍滞后\",\"relevance\":0.76}", topic
    printf "],\"total_score\":88}\n"
}'
