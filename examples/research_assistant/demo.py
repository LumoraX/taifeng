"""深度调研助手 demo —— sequential 链式 pipeline pattern。

场景：

    research-lead (composite entry)
       ├─ ① call_skill source-collector  ← 候选来源采集
       │      └─ run_script mock_search        （sub turn: 6 条多类型来源 JSON）
       ├─ ② call_skill fact-extractor    ← 把 sources 提炼为结构化 facts
       │      └─ run_script extract_facts      （sub turn: 5 条 fact JSON）
       └─ ③ call_skill report-writer     ← 基于 facts 写 outline
              └─ run_script draft_outline      （sub turn: 4 章 outline JSON）
    → research-lead 综合三步链式输出 → 完整调研报告

演示价值：sequential dependency —— 步骤 2 输入 = 步骤 1 输出，步骤 3 输入 = 步骤 2 输出。
跟 travel_planner 的并行 fan-out 互补，演示框架同样能驱动严格串行流水。

可视化:
    [USER >>>]    调研议题
    [SKILL DISP]  按 ①→②→③ 顺序派发，不会并发
    [TOOL CALL]   每个子 skill 内部 run_script 一次拿结构化 JSON
    [LLM FINAL]   research-lead 综合三段输出

运行（真实 LLM）：

    cd taifeng
    PYTHONPATH=src uv run python examples/research_assistant/demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SKILLS_DIR = HERE / "skills"

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
sys.path.insert(0, str(HERE.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()


import taifeng  # noqa: E402
from taifeng.skill.scripts.python import PythonScriptExecutor  # noqa: E402
from taifeng.skill.scripts.shell import ShellScriptExecutor  # noqa: E402


class C:
    """终端 ANSI 颜色常量。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    USER = "\033[96m"
    LLM = "\033[92m"
    TOOL_IN = "\033[93m"
    TOOL_OUT = "\033[33m"
    SKILL = "\033[95m"
    FINAL = "\033[1;92m"
    DIVIDER = "\033[90m"


def _hr(label: str = "") -> str:
    """渲染分隔线，可带标签。"""
    line = "─" * 70
    if label:
        return f"{C.DIVIDER}{line} {label} {line[:5]}{C.RESET}"
    return f"{C.DIVIDER}{line}{C.RESET}"


async def run(pool: taifeng.EnginePool, user_message: str) -> None:
    """跑一次 turn 并把所有 EventMsg 渲染到 stdout。

    通过 ``completed >= 4`` 判定结束：1 次父 turn + 3 次子 turn（每步 sub-skill 一次）。
    """
    print()
    print(_hr("深度调研（sequential pipeline ①→②→③）"))
    print(f"{C.USER}[USER >>>]{C.RESET}")
    for line in user_message.splitlines():
        print(f"  {line}")
    print(_hr())

    engine = await pool.get_or_create(
        session_id="research-assistant-demo", entry_skill_id="research-lead",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text=user_message))

    final_text = ""
    cur_buf = ""
    completed = 0
    in_parent_turn = False
    async for ev in engine.subscribe(sub_id):
        kind = ev.msg.kind
        data = ev.msg.data

        if kind == "turn_started":
            in_parent_turn = data.get("entry_skill_id") == "research-lead"
            print(
                f"{C.DIM}[TURN START]{C.RESET} "
                f"skill={C.BOLD}{data.get('entry_skill_id')}{C.RESET}"
            )

        elif kind == "assistant_text":
            delta = data.get("delta", "")
            cur_buf += delta
            while "\n" in cur_buf:
                line, cur_buf = cur_buf.split("\n", 1)
                print(f"{C.LLM}[LLM TEXT]{C.RESET} {line}")
            if in_parent_turn:
                final_text += delta

        elif kind == "tool_call_started":
            if cur_buf:
                print(f"{C.LLM}[LLM TEXT]{C.RESET} {cur_buf}")
                cur_buf = ""
            args_raw = data.get("arguments", "{}")
            try:
                args_pretty = json.dumps(
                    json.loads(args_raw), ensure_ascii=False, indent=2,
                )
            except (json.JSONDecodeError, TypeError):
                args_pretty = args_raw
            print(
                f"{C.TOOL_IN}[TOOL CALL]{C.RESET} "
                f"{C.BOLD}{data.get('name')}{C.RESET}  "
                f"call_id={data.get('call_id', '')[:12]}"
            )
            indented = "  " + args_pretty.replace("\n", "\n  ")
            print(f"{C.DIM}{indented}{C.RESET}")

        elif kind == "tool_call_completed":
            err = data.get("is_error")
            status = "✗ ERROR" if err else "✓ ok"
            color = "\033[91m" if err else C.TOOL_OUT
            output = data.get("output", "")
            print(
                f"{color}[TOOL DONE]{C.RESET} "
                f"{C.BOLD}{data.get('name')}{C.RESET}  {status}  "
                f"{data.get('duration_ms', 0)}ms"
            )
            for line in (output.splitlines() or [output])[:10]:
                print(f"{C.DIM}  | {line}{C.RESET}")

        elif kind == "skill_dispatched":
            chain = " ▶ ".join(data.get("stack_path") or [])
            print(
                f"{C.SKILL}[SKILL DISP]{C.RESET} ▶ "
                f"{C.BOLD}{data.get('skill_id')}{C.RESET}  "
                f"depth={data.get('depth')}  chain={chain}"
            )

        elif kind == "skill_returned":
            ok_mark = "✓" if data.get("success") else "✗"
            summary = data.get("summary", "")
            print(
                f"{C.SKILL}[SKILL RET]{C.RESET} ◀ "
                f"{C.BOLD}{data.get('skill_id')}{C.RESET}  {ok_mark}"
            )
            for line in summary.splitlines()[:6]:
                print(f"{C.DIM}  | {line}{C.RESET}")
            if len(summary.splitlines()) > 6:
                more = len(summary.splitlines()) - 6
                print(f"{C.DIM}  | ... (+{more} lines){C.RESET}")
            in_parent_turn = True

        elif kind == "turn_completed":
            if cur_buf:
                print(f"{C.LLM}[LLM TEXT]{C.RESET} {cur_buf}")
                cur_buf = ""
            usage = data.get("usage", {})
            print(
                f"{C.DIM}[TURN ✓]{C.RESET} iter={data.get('iterations')} "
                f"dur={data.get('duration_ms')}ms "
                f"in={usage.get('input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)}"
            )
            completed += 1
            # 父 turn + 3 个子 turn（source-collector / fact-extractor / report-writer）
            if completed >= 4:
                break

        elif kind == "turn_failed":
            print(f"\033[91m[TURN ✗] {data}\033[0m")
            break

    print(_hr())
    print(f"{C.FINAL}[LLM FINAL ANSWER]{C.RESET}")
    print(final_text)
    print(_hr())


async def main() -> int:
    """demo 入口：装 client → 建 pool → 跑一次调研 → 关闭 pool。"""
    try:
        client, meta = build_model_client()
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(
        f"{C.DIM}[setup] provider={meta['provider']} "
        f"model={meta['model']} key={meta.get('api_key_tail', '-')}{C.RESET}"
    )
    print(f"{C.DIM}[setup] skills={SKILLS_DIR}{C.RESET}")

    storage_dir = HERE / ".runs"
    storage_dir.mkdir(parents=True, exist_ok=True)

    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=storage_dir,
        model_client=client,
        compressors=[],
        script_executors={
            "shell": ShellScriptExecutor(),
            "python": PythonScriptExecutor(),
        },
    )

    try:
        await run(
            pool,
            user_message=(
                "请围绕「AI agent 框架的市场格局与瓶颈」做一次调研。\n"
                "请严格按照工作流程执行三步：\n"
                "① 调 source-collector 采集候选来源（max_sources=6）\n"
                "② 把 source-collector 返回的 candidates JSON 字符串原样\n"
                "  转发给 fact-extractor 做事实提炼\n"
                "③ 把 fact-extractor 返回的 facts JSON 字符串原样\n"
                "  转发给 report-writer 生成 outline\n"
                "最后综合三段输出一份完整调研报告。"
            ),
        )
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
