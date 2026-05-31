"""旅行规划 demo —— 经典 fan-out + 综合 pattern。

场景：

    trip-planner (composite entry)
       ├─ call_skill flights-finder       ← 派发到航班搜索
       │     └─ run_script mock_flights    （sub turn: 返候选 JSON）
       ├─ call_skill hotels-finder        ← 派发到酒店搜索
       │     └─ run_script mock_hotels
       └─ call_skill activities-finder    ← 派发到活动 / 景点搜索
             └─ run_script mock_activities
    → trip-planner 综合 3 路候选为「按日行程表」

演示价值：fan-out 三路并行子 turn + 父 skill 综合输出结构化方案。
这是 multi-agent agent 框架最经典的 pattern（旅行 / 调研 / 评审都用得上）。

可视化:
    [USER >>>]    用户输入（含目的地、日期、人数、预算）
    [TOOL CALL]   call_skill / run_script + 完整 args
    [SKILL DISP]  派发到子 skill（应触发 3 次）
    [LLM TEXT]    流式 token
    [SKILL RET]   子 skill 返回（每路一份候选）
    [LLM FINAL]   trip-planner 综合行程表

运行（真实 LLM）：

    cd taifeng
    PYTHONPATH=src uv run python examples/travel_planner/demo.py
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

    通过 ``completed >= 4`` 判定结束：1 次父 turn + 3 次子 turn（每路 finder 一次）。
    """
    print()
    print(_hr("旅行规划（fan-out 3 路 + 综合）"))
    print(f"{C.USER}[USER >>>]{C.RESET}")
    for line in user_message.splitlines():
        print(f"  {line}")
    print(_hr())

    engine = await pool.get_or_create(
        session_id="travel-planner-demo", entry_skill_id="trip-planner",
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
            in_parent_turn = data.get("entry_skill_id") == "trip-planner"
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
            # 父 turn + 3 个子 turn（每个 finder 一次）= 4 次
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
    """demo 入口：装 client → 建 pool → 跑一次旅行规划 → 关闭 pool。"""
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
                "帮我规划一次 9 月 12-15 日从北京到巴黎的 3 天旅行。\n"
                "- 出发: 北京\n"
                "- 目的地: 巴黎\n"
                "- 入住: 2026-09-12，退房: 2026-09-15\n"
                "- 人数: 2 人\n"
                "- 预算: ¥15000 人均\n"
                "- 兴趣: 美食、博物馆、夜景\n"
                "请按工作流程 fan-out 三个子 skill，最后给一份完整的按日行程方案。"
            ),
        )
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
