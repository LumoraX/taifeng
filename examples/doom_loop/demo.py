"""doom-loop 检测 demo —— 重复同调用空转的「先警后断」（纯 SimClient，无需 API key，ADR 0021）。

场景：模型反复以**相同参数**调用同一工具、每次都成功、毫无进展（doom-loop）。
守卫策略（先警后断 escalate）：

    连续 N 次同 (tool, args) 成功 ──▶ warn：注一条中性事实给模型自改，turn 续跑
    warn 后到 2N 仍重复     ──▶ open：断路，turn 以 end_reason=doom_loop_circuit_open 终止

演示价值：
    `DenialBreaker`（连续 deny 断路）和 `IterationBudget`（总迭代上限）都盖不到
    「重复成功空转」这个盲区——doom-loop 守卫专补它。中性事实不含产品祈使（R1）。

真实 LLM 版（强指令逼真模型连续重复，真触发 warn→open）：examples/real_llm/doom_verify.py

运行：
    cd taifeng
    PYTHONPATH=src uv run python examples/doom_loop/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.doom_loop import DoomLoopConfig
from taifeng.tool.spec import ToolResult, ToolSpec

_ENTRY = """---
name: doom-entry
description: doom-loop demo 入口
version: 1.0.0
type: composite
entry: true
child_skills: [noop]
tool_names: [ping]
max_call_depth: 2
---
# doom-loop demo 入口
机械探针任务：反复调用工具 `ping`（参数 `{}`）。
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
    """恒返回相同结果的工具 —— 构成「同调用同结果」的空转。"""
    async def _h(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("pong")

    return ToolSpec(name="ping", description="探针（恒返回 pong）",
                    input_schema={"type": "object", "properties": {}},
                    handler=_h, parallel_safe=True)


def _client() -> SimClient:
    """脚本：连续 4 次相同 ping（{}），再收尾。max_consecutive_repeats=2 →
    第 2 次 warn、第 4 次断路。"""
    return SimClient(turns=[
        SimTurn(text=f"ping#{i}", tool_calls=[{
            "id": f"d{i}", "name": "ping", "arguments": "{}",
        }])
        for i in range(1, 5)
    ] + [SimTurn(text="完成")])


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        for sub, body in {"doom-entry": _ENTRY, "noop": _NOOP}.items():
            (skills / sub).mkdir(parents=True)
            (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")

        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=root / "t", model_client=_client(),
            compressors=[], extra_tools=[_ping_tool()],
            doom_loop_config=DoomLoopConfig(max_consecutive_repeats=2),
            max_iterations=12,
        )
        engine = await pool.get_or_create(
            session_id="doom", entry_skill_id="doom-entry"
        )
        events: list = []

        async def watch():
            async for ev in engine.subscribe_all():
                events.append(ev.msg)
                if ev.msg.kind == "shutdown":
                    break

        task = asyncio.create_task(watch())
        await asyncio.sleep(0)
        await engine.submit(taifeng.UserMessage(text="开始探针任务。"))
        for _ in range(300):
            if any(m.kind in ("turn_completed", "turn_failed") for m in events):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        pings = sum(1 for m in events if m.kind == "tool_call_completed")
        warned = [m for m in events if m.kind == "doom_loop_warned"]
        opened = [m for m in events if m.kind == "doom_loop_circuit_open"]
        done = [m for m in events if m.kind == "turn_completed"]
        er = done[0].data.get("end_reason") if done else "?"

        print("=" * 60)
        print("doom-loop 先警后断（max_consecutive_repeats=2）")
        print("=" * 60)
        print(f"  ping 调用次数            = {pings}")
        print(f"  doom_loop_warned         = {len(warned)}  (连续 2 次同签名)")
        print(f"  doom_loop_circuit_open   = {len(opened)}  (到 4 次断路)")
        print(f"  turn end_reason          = {er}")
        ok = warned and opened and er == "doom_loop_circuit_open"
        print(f"  ==> {'✅ warn → open → 终止' if ok else '❌'}")

        await engine.submit(taifeng.loop.Shutdown())
        await asyncio.wait_for(task, timeout=5.0)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
