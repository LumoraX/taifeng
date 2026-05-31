#!/usr/bin/env python3
"""format_diff.py —— 把"原代码 + 建议代码"渲染成 unified diff。

Taifeng 把 args 字典按 args_schema.properties 顺序展开为 positional argv：

    argv[1] = before  (string)
    argv[2] = after   (string)
    argv[3] = label   (string, 可选)
"""

from __future__ import annotations

import difflib
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: format_diff.py <before> <after> [label]",
            file=sys.stderr,
        )
        return 2
    before = sys.argv[1]
    after = sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else "code"

    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label}.before",
        tofile=f"{label}.after",
        n=3,
    )
    sys.stdout.write("".join(diff_lines))
    if not before.endswith("\n") or not after.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
