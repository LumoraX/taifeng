#!/usr/bin/env python3
"""risk_score.py —— 代码审查风险评分（简化版）。

Taifeng 的 ``run_script`` 工具会按 SKILL.md 中 ``args_schema.properties`` 的
**字段顺序**展开 ``args`` 字典为 positional argv。本脚本对应的 args_schema
properties 顺序为：``loc, complexity, has_silent_fallback, has_magic_number``，因此：

    argv[1] = loc                 (number)
    argv[2] = complexity          (low | medium | high)
    argv[3] = has_silent_fallback ("true" | "false")
    argv[4] = has_magic_number    ("true" | "false")

输出 JSON：``{"score": 0-100, "level": "low|medium|high", "rationale": "..."}``。
"""

from __future__ import annotations

import json
import sys


def _to_bool(s: str) -> bool:
    return s.strip().lower() in {"true", "1", "yes"}


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(
            "usage: risk_score.py <loc> <complexity> "
            "[has_silent_fallback true/false] [has_magic_number true/false]",
            file=sys.stderr,
        )
        return 2

    try:
        loc = float(argv[0])
    except ValueError:
        print(f"error: loc must be a number, got {argv[0]!r}", file=sys.stderr)
        return 2

    complexity = argv[1]
    if complexity not in {"low", "medium", "high"}:
        print(
            f"error: complexity must be low|medium|high, "
            f"got {complexity!r}",
            file=sys.stderr,
        )
        return 2

    has_silent_fallback = _to_bool(argv[2]) if len(argv) > 2 else False
    has_magic_number = _to_bool(argv[3]) if len(argv) > 3 else False

    # LOC 贡献：0 ~ 60
    if loc < 30:
        loc_score = 5
    elif loc < 80:
        loc_score = 20
    elif loc < 200:
        loc_score = 40
    elif loc < 500:
        loc_score = 55
    else:
        loc_score = 60

    # 圈复杂度贡献：0 ~ 20
    complexity_score = {
        "low": 5,
        "medium": 15,
        "high": 20,
    }[complexity]

    morph_score = 0
    if has_silent_fallback:
        morph_score += 10
    if has_magic_number:
        morph_score += 10

    total = min(loc_score + complexity_score + morph_score, 100)
    if total < 30:
        level = "low"
    elif total < 60:
        level = "medium"
    else:
        level = "high"

    rationale_parts = [
        f"loc={loc}(+{loc_score})",
        f"complexity={complexity}(+{complexity_score})",
    ]
    if has_silent_fallback:
        rationale_parts.append("silent_fallback(+10)")
    if has_magic_number:
        rationale_parts.append("magic_number(+10)")

    result = {
        "score": total,
        "level": level,
        "rationale": " · ".join(rationale_parts),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
