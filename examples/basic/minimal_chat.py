"""最小可运行示例 —— 单一 entry skill + Mock 客户端 + 控制台输出。

运行：
    cd taifeng
    PYTHONPATH=src python examples/basic/minimal_chat.py

预期输出（claw-code 风格）：
    [hh:mm:ss] turn → sub=... entry=code-reviewer model=mock-model
    [hh:mm:ss] tool → read_skill(...) call_id=...
    [hh:mm:ss] tool ← read_skill ok (Nms): ...
    [hh:mm:ss] turn ✓ iter=2 dur=... in=550 out=80 cached=180 (33%) end=completed
    [hh:mm:ss] llm  ← 基于解剖信息，结节位于...
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.telemetry import attach_console_sink


ATOMIC = """---
name: style-checker
description: 代码风格规则
version: 1.0.0
type: atomic
---
# 风格规则
函数 ≤ 80 行；命名走 snake_case；圈复杂度 ≤ 10...
"""

COMPOSITE = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家

你是一位代码审查专家。

可用工具：
- `read_skill(skill_id)` —— 读取子 skill 的完整文档
- `call_skill(skill_id, args)` —— 派发到子 skill 执行

需要风格规范时调用 `read_skill("style-checker")` 获取参考规则。
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "style-checker").mkdir(parents=True)
        (skills / "style-checker" / "SKILL.md").write_text(ATOMIC, encoding="utf-8")
        (skills / "code-reviewer").mkdir(parents=True)
        (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE, encoding="utf-8")

        # Mock LLM 脚本：
        #   turn 1: 决定调用 read_skill
        #   turn 2: 工具返回后给最终答复
        client = MockClient(turns=[
            MockTurn(
                text="正在加载风格规则...",
                tool_calls=[{
                    "id": "tc_1",
                    "name": "read_skill",
                    "arguments": '{"skill_id": "style-checker"}',
                }],
                usage=TokenUsage(input_tokens=200, output_tokens=30, total_tokens=230),
            ),
            MockTurn(
                text="函数行数 120 > 80 上限，建议拆分；命名 getCwd 不符合 snake_case。",
                usage=TokenUsage(input_tokens=380, output_tokens=55, total_tokens=435,
                                 cache_read_input_tokens=190),
            ),
        ])

        # ① 进程级单例
        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=threads,
            model_client=client,
            compressors=[],  # 演示极简流程，关闭压缩
        )

        # ② 会话级 Engine
        engine = await pool.get_or_create(
            session_id="demo-session",
            entry_skill_id="code-reviewer",
            cwd=str(Path.cwd()),
        )

        # ③ 订阅控制台输出（类似 claw-code）
        sink_task = attach_console_sink(engine, color=True)

        # ④ 提交用户消息
        sub_id = await engine.submit(taifeng.UserMessage(
            text="请审查这段代码：def getCwd(x): ... （120 行函数）",
        ))

        # ⑤ 等待 turn 完成
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        await pool.close()
        await asyncio.sleep(0.1)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
