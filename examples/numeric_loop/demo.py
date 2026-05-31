"""数值调谐震荡回归 demo —— 真正的 LLM loop 测试。

测试要点:LLM 是否能自主多轮调用同一工具,跨轮在对话历史里跟踪状态:

    1. 收到 (current=10, target=50)
    2. 调 run_script("apply_delta", {current, target}) → 拿到 new (带随机噪声)
    3. 判断 gap < 0.5?
       - 收敛  → 停,输出调谐报告(含完整轨迹)
       - 未收敛 → 把 new 作 current,**继续调** apply_delta
    4. 每次 delta 是 sign(target-current) * uniform(0.3, 1.5) * |diff| * 0.4
       —— 有概率 overshoot,所以可能震荡(在 target 两侧来回)
    5. 最多 12 轮强制结束

可视化:
    [USER >>>]   初始 (current, target)
    [TOOL CALL]  每轮 LLM 传的 args（直接看 current 是否正确从上一轮继承）
    [TOOL DONE]  每轮 script 输出（old / delta / new / gap）
    [LLM FINAL]  最终调谐报告

运行（真实 LLM）:

    cd taifeng
    PYTHONPATH=src uv run python examples/numeric_loop/demo.py
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


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    USER = "\033[96m"
    LLM = "\033[92m"
    TOOL_IN = "\033[93m"
    TOOL_OUT = "\033[33m"
    FINAL = "\033[1;92m"
    DIVIDER = "\033[90m"
    HIGHLIGHT = "\033[1;33m"


def _hr(label: str = "") -> str:
    line = "─" * 70
    if label:
        return f"{C.DIVIDER}{line} {label} {line[:5]}{C.RESET}"
    return f"{C.DIVIDER}{line}{C.RESET}"


async def run(
    pool: taifeng.EnginePool,
    *,
    initial_current: float,
    target: float,
    max_iter_hint: int = 12,
) -> None:
    user_message = (
        f"请把 current 调谐到 target。\n"
        f"  - current = {initial_current}\n"
        f"  - target  = {target}\n"
        f"严格按工作流执行,反复调 apply_delta 直到 gap < 0.5 或 12 轮上限。\n"
        f"最后给【调谐报告】,包含完整轨迹列表。"
    )
    print()
    print(_hr(f"数值调谐震荡回归 (current={initial_current} → target={target})"))
    print(f"{C.USER}[USER >>>]{C.RESET}")
    for line in user_message.splitlines():
        print(f"  {line}")
    print(_hr())

    engine = await pool.get_or_create(
        session_id="numeric-loop", entry_skill_id="numeric-tuner",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text=user_message))

    final_text = ""
    cur_buf = ""
    # 用于轨迹检查：从每次 tool_call_completed 解析 new
    trace: list[dict] = []

    async for ev in engine.subscribe(sub_id):
        kind = ev.msg.kind
        data = ev.msg.data

        if kind == "turn_started":
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
            final_text += delta

        elif kind == "tool_call_started":
            if cur_buf:
                print(f"{C.LLM}[LLM TEXT]{C.RESET} {cur_buf}")
                cur_buf = ""
            args_raw = data.get("arguments", "{}")
            try:
                args_pretty = json.dumps(
                    json.loads(args_raw), ensure_ascii=False,
                )
            except (json.JSONDecodeError, TypeError):
                args_pretty = args_raw
            print(
                f"{C.TOOL_IN}[TOOL CALL #{len(trace)+1}]{C.RESET} "
                f"{C.BOLD}{data.get('name')}{C.RESET}"
            )
            print(f"  {C.DIM}{args_pretty}{C.RESET}")

        elif kind == "tool_call_completed":
            err = data.get("is_error")
            output = data.get("output", "")
            try:
                payload = json.loads(output)
                trace.append(payload)
                # 高亮关键字段
                old, dlt, new, gap = (
                    payload.get("old"), payload.get("delta"),
                    payload.get("new"), payload.get("gap"),
                )
                status_mark = (
                    f"{C.HIGHLIGHT}★ CONVERGED{C.RESET}"
                    if gap is not None and gap < 0.5
                    else f"gap={gap}"
                )
                print(
                    f"{C.TOOL_OUT}[TOOL DONE #{len(trace)}]{C.RESET} "
                    f"old={old} {C.DIM}+{C.RESET}{C.HIGHLIGHT}{dlt:+.3f}{C.RESET}"
                    f" {C.DIM}={C.RESET} new={C.BOLD}{new}{C.RESET}  "
                    f"{status_mark}"
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                marker = "✗ ERROR" if err else "✓ ok"
                print(
                    f"{C.TOOL_OUT}[TOOL DONE]{C.RESET} {marker} "
                    f"{data.get('duration_ms', 0)}ms"
                )
                print(f"  {C.DIM}{output[:200]}{C.RESET}")

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
            break

        elif kind == "turn_failed":
            print(f"\033[91m[TURN ✗] {data}\033[0m")
            break

    print(_hr("轨迹（脚本视角）"))
    if trace:
        path = [initial_current] + [t.get("new") for t in trace]
        formatted = " → ".join(f"{v:.2f}" for v in path)
        print(f"  {formatted}")
        last = trace[-1]
        gap = last.get("gap", float("inf"))
        converged = gap < 0.5
        print(
            f"  共 {len(trace)} 轮,最终 new={last.get('new')}, "
            f"gap={gap}, 收敛={converged}"
        )
    else:
        print("  (没有 tool call 记录)")

    print(_hr())
    print(f"{C.FINAL}[LLM FINAL ANSWER]{C.RESET}")
    print(final_text)
    print(_hr())


async def main() -> int:
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

    # max_iterations 设大点,让 LLM 有空间反复调 apply_delta(默认 32)
    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=storage_dir,
        model_client=client,
        compressors=[],
        max_iterations=30,
        script_executors={"python": PythonScriptExecutor()},
    )

    try:
        await run(pool, initial_current=10.0, target=50.0)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
