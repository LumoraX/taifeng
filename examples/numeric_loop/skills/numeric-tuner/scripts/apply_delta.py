#!/usr/bin/env python3
"""apply_delta.py —— 朝 target 方向施加带噪声的增量（震荡回归）。

Taifeng 把 args 字典按 args_schema.properties 顺序展开为 positional argv：

    argv[1] = current  (float)
    argv[2] = target   (float)

输出 JSON:
    {"old": current, "delta": d, "new": current+d,
     "gap": abs(current+d - target), "target": target}

公式：
    direction = sign(target - current)
    magnitude = uniform(0.3, 1.5) * abs(target - current) * 0.4
    delta     = direction * magnitude

平均步长 = 40% 距离，但 0.3-1.5 倍随机系数 → 有概率 overshoot，
迫使 LLM 多轮调用才能收敛到 ±0.5 容差。
"""

from __future__ import annotations

import json
import random
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: apply_delta.py <current> <target>", file=sys.stderr)
        return 2
    try:
        current = float(sys.argv[1])
        target = float(sys.argv[2])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    diff = target - current
    if abs(diff) < 1e-9:
        delta = 0.0
    else:
        direction = 1.0 if diff > 0 else -1.0
        magnitude = random.uniform(0.3, 1.5) * abs(diff) * 0.4
        delta = direction * magnitude

    new = current + delta
    gap = abs(new - target)

    result = {
        "old": round(current, 3),
        "delta": round(delta, 3),
        "new": round(new, 3),
        "gap": round(gap, 3),
        "target": target,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
