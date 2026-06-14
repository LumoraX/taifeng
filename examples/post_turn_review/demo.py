"""PostTurn 钩子 demo —— turn 收尾自我审计 / 记忆固化(userspace 范式)。

场景:多轮对话,每轮分析做完后,业务侧用 post_turn 钩子**确定性地**在下一轮开始
前固化「本轮要记住什么」—— 这正是 post_turn 相对订阅 turn_completed 事件的增量:
**顺序保证**(下一 turn 启动前必然完成)。

要点(对照 ADR 0019):
    - post_turn 是内核只提供的 **seam**(审计型,不可否决,root turn 真终态触发);
    - review / 固化的**内容**(存什么、怎么存)全在 userspace —— 这里用一个进程内
      list 模拟「长期记忆」,真实业务可换成 memory_store.writeback / 向量库 / DB;
    - 钩子同步执行,保证「固化完才进下一轮」。挂起 / 取消的 turn 不触发(非真终态)。

钩子 vs 事件订阅(本 demo 的核心对照):
    - 订阅 turn_completed 事件:fire-and-forget,无法保证下一轮前跑完(异步旁路);
    - post_turn 钩子:同步、在 turn 边界内,给「下一轮前必须完成」的顺序保证。

运行(SimClient,**无需 API key**):

    cd taifeng
    PYTHONPATH=src uv run python examples/post_turn_review/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

_ENTRY_SKILL = """---
name: analyst
description: 分析助手
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: []
tool_names: [file_read]
max_call_depth: 2
---
# 分析助手
逐轮分析用户问题并给出结论。
"""


# 进程内「长期记忆」—— 真实业务换成 memory_store.writeback / 向量库 / DB。
CONSOLIDATED: list[str] = []


def _build_post_turn_runner() -> HookRunner:
    """注册一个 post_turn 钩子:每轮真终态后把本轮结论固化进长期记忆。"""
    reg = HookRegistry()

    async def consolidate(hook, ctx) -> HookDecision:
        # hook.final_text = 本轮最终结论;hook.iteration = 轮次;ctx.extras['cancel'] = R4 token
        note = f"[turn {hook.iteration}] 固化结论: {hook.final_text}"
        CONSOLIDATED.append(note)
        print(f"  ↳ post_turn 触发(end_reason={hook.end_reason}) → {note}")
        return HookDecision.ok()  # 审计型:返回值不影响已终结的 turn

    reg.register("post_turn", consolidate)
    return HookRunner(reg)


async def main() -> None:
    """跑两轮对话,观察每轮收尾 post_turn 在下一轮前确定性固化记忆。"""
    with tempfile.TemporaryDirectory() as td:
        skills = Path(td) / "skills"
        (skills / "analyst").mkdir(parents=True)
        (skills / "analyst" / "SKILL.md").write_text(_ENTRY_SKILL, encoding="utf-8")
        threads = Path(td) / "threads"

        # 两轮:每轮 SimClient 给一条结论文本
        _u = TokenUsage(input_tokens=10, output_tokens=4)
        client = SimClient(turns=[
            SimTurn(text="结论一:指标 A 偏高", usage=_u),
            SimTurn(text="结论二:结合上轮,建议复查 A", usage=_u),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=threads,
            model_client=client, compressors=[],
            hooks=_build_post_turn_runner(),  # 注入 post_turn 钩子
        )
        engine = await pool.get_or_create(
            session_id="demo-post-turn", entry_skill_id="analyst",
        )

        for i, text in enumerate(["分析一下指标 A", "那要不要复查?"]):
            print(f"\n用户轮 {i}: {text}")
            sub_id = await engine.submit(taifeng.UserMessage(text=text))
            # 等本轮终态(post_turn 在 turn_completed 之后、下一轮前同步触发)
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break
            await asyncio.sleep(0.05)  # 让 post_turn(turn_completed 之后)落定再进下一轮

        await pool.close()

    print("\n=== 固化进长期记忆的内容(post_turn 顺序保证,每轮一条)===")
    for note in CONSOLIDATED:
        print(" ", note)
    assert len(CONSOLIDATED) == 2, CONSOLIDATED  # 两轮各固化一次


if __name__ == "__main__":
    asyncio.run(main())
