"""差异化授权 demo —— 按 skill 粒度精细权限策略。

场景：

    analysis-orchestrator (composite entry)
       ├─ call_skill prd-evaluator   ← **白名单 allow**：静默执行，不弹窗
       │      └─ run_script prd_check
       └─ call_skill swot-evaluator  ← **ask**：弹审批窗口，用户允许后执行
              └─ run_script swot_screen
    → orchestrator 综合两路输出

演示价值：和 permission_showcase（全通过）+ code_review（全询问）形成三极对比，
本 demo 演示**中间地带 —— 按 skill 粒度精细授权**。生产里最常见的模式：
"便宜 / 安全"的 skill 静默执行，"贵 / 改世界"的 skill 强制审批。

权限配置（见 web_ui server.py 的 policy_config_overrides）：

    {
        "default_mode": "allow",
        "allow": [
            "FileRead(*)", "FileWrite(/tmp/*)",
            "Skill(prd-evaluator)",   # ← 这一行让 prd-evaluator 静默
        ],
        "ask": [
            "Skill(swot-evaluator)",  # ← 这一行让 swot-evaluator 弹窗
        ],
        "deny": ["Bash(rm -rf)", "Bash(sudo)"],
    }

可视化:
    时间轴预期事件：
      tool_call_started call_skill(prd-evaluator)
      skill_dispatched → prd-evaluator           ← **不弹 HITL**
      turn_started entry=prd-evaluator
      ... 子 turn 内事件 ...
      turn_completed [sub]
      skill_returned ← prd-evaluator ✓
      tool_call_started call_skill(swot-evaluator)
      hitl_required skill_dispatch: swot-evaluator  ← **弹窗等审批**
      （用户点允许）
      skill_dispatched → swot-evaluator
      ... 子 turn 内事件 ...

运行（真实 LLM）：

    cd taifeng
    PYTHONPATH=src uv run python examples/selective_approval/demo.py
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
from taifeng.permission import (  # noqa: E402
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
)
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
    HITL = "\033[91m"
    FINAL = "\033[1;92m"
    DIVIDER = "\033[90m"


def _hr(label: str = "") -> str:
    """渲染分隔线，可带标签。"""
    line = "─" * 70
    if label:
        return f"{C.DIVIDER}{line} {label} {line[:5]}{C.RESET}"
    return f"{C.DIVIDER}{line}{C.RESET}"


async def _cli_prompter_callback(req: Any) -> PermissionDecision:  # type: ignore[valid-type]
    """CLI 版 HITL prompter：终端打印请求详情，用户在 stdin 输入 y/n。

    web_ui demo 里 prompter 通过 SSE 推前端弹窗；本 CLI demo 走 stdin
    交互，让用户在跑 demo 时直接感受到"哪个 skill 被静默 / 哪个被询问"。
    """
    print()
    print(f"{C.HITL}══ HITL 审批 ══{C.RESET}")
    print(f"  scope    : {req.scope}")
    print(f"  target   : {req.target}")
    print(f"  reason   : {req.reason or '(LLM 未填)'}")
    print(f"  chain    : {' → '.join(req.call_chain or [])}")
    sys.stdout.flush()
    # 同步读 stdin（CLI demo 阻塞可接受）
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(
        None, lambda: input(f"{C.HITL}允许？[y/N]: {C.RESET}").strip().lower(),
    )
    granted = answer in ("y", "yes", "是", "允许")
    print()
    return (
        PermissionDecision.allow(reason="cli_user_allow", remember="once")
        if granted
        else PermissionDecision.deny(reason="cli_user_deny", remember="once")
    )


async def run(pool: taifeng.EnginePool, user_message: str) -> None:
    """跑一次 turn 并把所有 EventMsg 渲染到 stdout。

    通过 ``completed >= 3`` 判定结束：1 次父 turn + 2 次子 turn。
    """
    print()
    print(_hr("差异化授权（prd 静默 / swot 弹窗）"))
    print(f"{C.USER}[USER >>>]{C.RESET}")
    for line in user_message.splitlines():
        print(f"  {line}")
    print(_hr())

    engine = await pool.get_or_create(
        session_id="selective-approval-demo",
        entry_skill_id="analysis-orchestrator",
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
            in_parent_turn = (
                data.get("entry_skill_id") == "analysis-orchestrator"
            )
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
            # 父 turn + 2 个子 turn（prd / swot）= 3 次
            if completed >= 3:
                break

        elif kind == "turn_failed":
            print(f"\033[91m[TURN ✗] {data}\033[0m")
            break

    print(_hr())
    print(f"{C.FINAL}[LLM FINAL ANSWER]{C.RESET}")
    print(final_text)
    print(_hr())


def _build_policy() -> PermissionPolicy:
    """组装差异化授权策略：prd-evaluator 静默放行，swot-evaluator 弹审批。

    顺序很重要：``PermissionPolicy.from_dict`` 按 deny → allow → ask 顺序编译，
    第一个命中即 short-circuit。所以 ``Skill(prd-evaluator)`` 在 allow 列表
    里先匹配命中 allow，不会再触达 ask 列表里的通用 ``Skill(*)``。
    """
    return PermissionPolicy.from_dict(
        {
            "default_mode": "allow",
            "deny": [
                "Bash(re:^rm\\s+-rf\\s+/)",
                "Bash(re:^sudo\\b)",
            ],
            "allow": [
                "FileRead(*)",
                "FileWrite(/tmp/*)",
                "Skill(prd-evaluator)",  # ← 这个子 skill 白名单，静默放行
                "Skill(read_*)",
            ],
            "ask": [
                "Skill(swot-evaluator)",  # ← 这个子 skill 必须弹窗审批
            ],
        },
        prompter=CallbackPrompter(_cli_prompter_callback),
        prompter_timeout_seconds=120.0,
    )


async def main() -> int:
    """demo 入口：装 client → 建 pool（带差异化策略）→ 跑分析 → 关闭 pool。"""
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
    print(
        f"{C.DIM}[setup] policy: prd-evaluator=allow / "
        f"swot-evaluator=ask{C.RESET}"
    )

    storage_dir = HERE / ".runs"
    storage_dir.mkdir(parents=True, exist_ok=True)

    pool = await taifeng.EnginePool.create(
        skills_dir=SKILLS_DIR,
        storage_dir=storage_dir,
        model_client=client,
        compressors=[],
        permission_policy=_build_policy(),
        script_executors={
            "shell": ShellScriptExecutor(),
            "python": PythonScriptExecutor(),
        },
    )

    try:
        await run(
            pool,
            user_message=(
                "请帮我同时做 PRD 评估 + SWOT 战略分析：\n\n"
                "[方案] 我们计划做一款 AI 编程助手，自研模型 + VSCode 多端集成，\n"
                "  瞄准实时辅助编程的新兴市场。\n"
                "[团队] 5 年 AI 工程经验，6 人小团队，自有专利 3 项。\n"
                "[目标] 12 个月内 1 万 DAU；订阅制商业模式。\n"
                "[范围] 代码补全、错误诊断、单元测试自动生成。\n"
                "[非目标] 不做 IDE 本身、不做企业部署版。\n"
                "[挑战] 大厂同类产品已就位、模型推理成本高、监管不确定。\n\n"
                "请按工作流程 fan-out 两个子 skill：\n"
                "  - prd-evaluator 评估 PRD 完整性 / 落地复杂度（白名单，静默）\n"
                "  - swot-evaluator 做 SWOT 战略分析（弹审批，请你点允许）\n"
                "最后整合成一份分析报告。"
            ),
        )
    finally:
        await pool.close()
    return 0


# Any 用于 prompter callback 签名，避免被 mypy 强制约束（cb 入参类型在
# permission.types 内部，跨包 import 在 CLI demo 不值得拉进来）
from typing import Any  # noqa: E402


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
