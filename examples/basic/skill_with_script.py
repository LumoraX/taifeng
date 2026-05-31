"""SKILL.md scripts 端到端示例 —— LLM 调用 run_script 跑 skill 内脚本。

演示：
    entry skill `data-prep` 声明 1 个 shell 脚本 + 1 个 python 脚本。
    MockClient 第一轮让 LLM 调 `run_script(skill_id="data-prep", script_name="prep")`，
    第二轮调 python validate 脚本，第三轮汇总输出。

运行：
    cd taifeng
    PYTHONPATH=src python examples/basic/skill_with_script.py

参见 ``docs/decisions/0009-scripts-runtime.md``。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.skill.scripts.python import PythonScriptExecutor
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.telemetry import attach_console_sink


HELPER_SKILL_MD = """---
name: helper
description: 辅助 skill（占位）
version: 1.0.0
type: atomic
---
# helper
"""


DATA_PREP_SKILL_MD = """---
name: data-prep
description: 数据预处理 skill —— 调用脚本完成 CSV 标准化与校验
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [helper]
tool_names: []
scripts:
  - name: prep
    path: scripts/prep.sh
    language: shell
    timeout_seconds: 30
    description: 标准化 CSV（trim / 去空行）
  - name: validate
    path: scripts/validate.py
    language: python
    timeout_seconds: 15
    description: 校验 CSV schema
---
# data-prep

你是数据预处理助手。先调 `run_script(script_name="prep")`，
再调 `run_script(script_name="validate")`，最后给出结论。
"""


PREP_SH = """#!/bin/sh
# 简单输出 —— 真实场景里会处理 stdin/文件
echo 'normalized-csv-output'
"""


VALIDATE_PY = """import json
result = {"rows": 100, "errors": 0, "schema_ok": True}
print(json.dumps(result))
"""


def _setup_workspace() -> Path:
    """搭一个最小 skill 工作目录。"""
    workspace = Path(tempfile.mkdtemp(prefix="taifeng-scripts-demo-"))
    skills = workspace / "skills"

    helper_dir = skills / "helper"
    helper_dir.mkdir(parents=True)
    (helper_dir / "SKILL.md").write_text(HELPER_SKILL_MD, encoding="utf-8")

    skill_dir = skills / "data-prep"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(DATA_PREP_SKILL_MD, encoding="utf-8")

    prep = skill_dir / "scripts" / "prep.sh"
    prep.write_text(PREP_SH, encoding="utf-8")
    prep.chmod(0o755)

    validate = skill_dir / "scripts" / "validate.py"
    validate.write_text(VALIDATE_PY, encoding="utf-8")
    validate.chmod(0o755)

    (workspace / "threads").mkdir()
    return workspace


def _mock_turns() -> list[MockTurn]:
    return [
        MockTurn(
            text="开始预处理。",
            tool_calls=[{
                "id": "c1",
                "name": "run_script",
                "arguments": (
                    '{"skill_id": "data-prep", '
                    '"script_name": "prep", "args": {}}'
                ),
            }],
            usage=TokenUsage(input_tokens=120, output_tokens=15),
        ),
        MockTurn(
            text="预处理完成，开始校验。",
            tool_calls=[{
                "id": "c2",
                "name": "run_script",
                "arguments": (
                    '{"skill_id": "data-prep", '
                    '"script_name": "validate", "args": {}}'
                ),
            }],
            usage=TokenUsage(input_tokens=160, output_tokens=20),
        ),
        MockTurn(
            text="校验通过：100 行 / 0 错误。",
            usage=TokenUsage(input_tokens=200, output_tokens=15),
        ),
    ]


async def main() -> None:
    workspace = _setup_workspace()
    print(f"workspace: {workspace}")
    print(f"skills:    {workspace / 'skills'}")

    client = MockClient(turns=_mock_turns())
    pool = await taifeng.EnginePool.create(
        skills_dir=workspace / "skills",
        threads_dir=workspace / "threads",
        model_client=client,
        compressors=[],
        script_executors={
            "shell": ShellScriptExecutor(),
            "python": PythonScriptExecutor(),
        },
    )

    # 控制台 sink 打印所有 EventMsg（含 5 类 script_execution_*）
    engine = await pool.get_or_create(
        session_id="demo", entry_skill_id="data-prep"
    )
    attach_console_sink(engine)

    sub_id = await engine.submit(
        taifeng.UserMessage(text="跑数据预处理并校验")
    )
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            print(f"\n=== outcome: {ev.msg.kind} ===")
            break

    # 检查 store 中的 function_call_output 验证 script 真跑了
    items = [
        it async for it in await pool.store.load_thread(engine.thread_id)
    ]
    print("\n=== thread items (kind=function_call_output) ===")
    for it in items:
        if it.kind == "function_call_output":
            output = it.payload.get("output", "")
            print(f"  call_id={it.payload['call_id']}: {output!r}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
