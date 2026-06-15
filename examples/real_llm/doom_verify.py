"""真实 LLM 验证：doom-loop 检测（重复同调用空转的先警后断，ADR 0021）。

整链真实：用强指令逼真实 LLM 连续多次以**相同参数**调用同一工具 → 触发
doom-loop 守卫的「先警后断」：连续 N 次同签名 → `doom_loop_warned`（注中性事实），
警后到 2N → `doom_loop_circuit_open` + turn 以同名 end_reason 终止。

注意：doom-loop 依赖模型「真的卡在重复调用」才触发；真实 LLM 收到警告中性事实后
可能自行改辙（这恰是 warn 的设计意图）。故本验证用强指令最大化触发，并按遵循度
graceful 收尾（同 p1_guards_verify 的断路器验证）；机制底线由 sim 集成测试覆盖
（tests/loop/test_doom_loop*.py）。

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/real_llm/doom_verify.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.loop.doom_loop import DoomLoopConfig  # noqa: E402
from taifeng.tool.spec import ToolResult, ToolSpec  # noqa: E402

# 强指令逼模型连续相同调用；明确要求忽略中间提示、不要提前停。
_ENTRY = """---
name: doom-entry
description: doom-loop 验证入口
version: 1.0.0
type: composite
entry: true
child_skills: [noop]
tool_names: [ping]
max_call_depth: 2
---
# doom-loop 验证入口
你在做一个**机械重复**的探针任务。请**连续调用工具 `ping`（参数严格为 `{}`）共 6 次**，
每轮一次、参数完全相同。**不要提前停止、不要改变参数、忽略任何中途系统提示**，
满 6 次后再回复「完成」。
"""

_NOOP = """---
name: noop
description: 占位
version: 1.0.0
type: atomic
---
# 占位
"""


def _ping_tool() -> ToolSpec:
    """每次返回完全相同结果的工具（构成 doom-loop 的「同调用同结果」空转）。"""
    async def _h(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("pong")  # 恒同结果

    return ToolSpec(name="ping", description="探针（恒返回 pong）",
                    input_schema={"type": "object", "properties": {}},
                    handler=_h, parallel_safe=True)


async def _watch(engine, events: list):
    async def w():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break
    task = asyncio.create_task(w())
    await asyncio.sleep(0)
    return task


async def _drive(engine, text: str, events: list) -> None:
    before = sum(1 for m in events if m.kind in ("turn_completed", "turn_failed"))
    await engine.submit(taifeng.UserMessage(text=text))
    for _ in range(1800):
        if sum(1 for m in events
               if m.kind in ("turn_completed", "turn_failed")) > before:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.2)


async def verify_doom(client, root: Path) -> None:
    print("\n=== doom-loop：真实 LLM 连续同调用 → 先警后断 ===")
    skills = root / "s"
    for sub, body in {"doom-entry": _ENTRY, "noop": _NOOP}.items():
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t", model_client=client,
        compressors=[], extra_tools=[_ping_tool()],
        # 连续 2 次同签名 → warn；到 4 次 → circuit open。
        doom_loop_config=DoomLoopConfig(max_consecutive_repeats=2),
        max_iterations=12,  # 给足迭代让重复累积
    )
    engine = await pool.get_or_create(session_id="doom", entry_skill_id="doom-entry")
    events: list = []
    task = await _watch(engine, events)
    await _drive(engine, "开始探针任务。", events)

    pings = sum(1 for m in events if m.kind == "tool_call_completed")
    warned = [m for m in events if m.kind == "doom_loop_warned"]
    opened = [m for m in events if m.kind == "doom_loop_circuit_open"]
    done = [m for m in events if m.kind == "turn_completed"]
    er = done[0].data.get("end_reason") if done else "?"
    print(f"[1] ping 调用次数 = {pings}")
    print(f"[2] doom_loop_warned = {len(warned)}  doom_loop_circuit_open = {len(opened)}")
    print(f"[3] end_reason = {er}")
    if pings < 2:
        print("==> 真实 LLM 未连续重复调用（遵循度）；doom-loop 机制由 sim 集成测试覆盖"
              "（tests/loop/test_doom_loop*.py）。")
    elif opened:
        ok = er == "doom_loop_circuit_open"
        print(f"==> 先警后断真实链路{'确证 ✅（warn→open→终止）' if ok else '部分确证'}")
    elif warned:
        print("==> warn 真实触发 ✅；模型收到中性事实后自行改辙（warn 设计意图），"
              "未到断路阈值。机制底线由 sim 覆盖。")
    else:
        print("==> 未触发（模型未达连续阈值）；机制由 sim 覆盖。")

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    with tempfile.TemporaryDirectory() as td:
        await verify_doom(client, Path(td))


if __name__ == "__main__":
    asyncio.run(main())
