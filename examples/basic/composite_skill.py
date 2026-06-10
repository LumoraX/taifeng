"""Composite skill 递归派发示例 —— call_skill 嵌套调用 + 调用栈 + 环检测。

演示：
    entry skill `programmer` 接到任务后，通过 `call_skill("code-review", ...)`
    派发到子 skill；子 skill 完成后结果回流，主 skill 给最终答复。

运行：
    cd taifeng
    PYTHONPATH=src python examples/basic/composite_skill.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.telemetry import attach_console_sink


CODE_REVIEW_SKILL = """---
name: code-review
description: 代码审查与改进建议
version: 1.0.0
type: atomic
---
# 代码审查

聚焦正确性、安全性、可读性。返回结构化建议。
"""

PROGRAMMER_SKILL = """---
name: programmer
description: 通用程序员
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [code-review]
tool_names: []
max_call_depth: 6
---
# 程序员

你是一位资深软件工程师。审查代码时通过 `call_skill("code-review", {"code": "..."})`
派发到代码审查专家。
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "code-review").mkdir(parents=True)
        (skills / "code-review" / "SKILL.md").write_text(CODE_REVIEW_SKILL, encoding="utf-8")
        (skills / "programmer").mkdir(parents=True)
        (skills / "programmer" / "SKILL.md").write_text(PROGRAMMER_SKILL, encoding="utf-8")

        # Mock：
        #  programmer turn 1 → 派 call_skill(code-review)
        #  code-review turn 1 → 给出审查建议（无 tool call）
        #  programmer turn 2 → 用子 skill 输出给最终答复
        client = SimClient(turns=[
            SimTurn(
                text="开始派发代码审查专家...",
                tool_calls=[{
                    "id": "tc_review",
                    "name": "call_skill",
                    "arguments": '{"skill_id": "code-review", "args": {"code": "def add(a,b): return a+b"}}',
                }],
                usage=TokenUsage(input_tokens=120, output_tokens=20, total_tokens=140),
            ),
            # 这条是 code-review 子 skill 的 turn 1
            SimTurn(
                text="审查结论：函数简洁，建议添加类型注解与 docstring。",
                usage=TokenUsage(input_tokens=80, output_tokens=20, total_tokens=100),
            ),
            # programmer turn 2 —— 工具结果回流
            SimTurn(
                text="综合建议：保留函数逻辑，补充 type hints 与文档字符串。",
                usage=TokenUsage(input_tokens=180, output_tokens=30, total_tokens=210,
                                 cache_read_input_tokens=100),
            ),
        ])

        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=threads, model_client=client, compressors=[],
        )
        engine = await pool.get_or_create(
            session_id="demo-composite",
            entry_skill_id="programmer",
        )
        sink_task = attach_console_sink(engine, color=True)

        sub_id = await engine.submit(taifeng.UserMessage(
            text="请审查这段代码：def add(a, b): return a + b",
        ))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        await pool.close()
        await asyncio.sleep(0.1)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
