"""Hooks demo —— 业务级 pre/post_skill_dispatch 钩子按运行时 args 动态拦截。

场景：

    task-runner (composite entry)
       └─ turn 1：call_skill data-export {"scope":"all"}
            → pre_skill_dispatch 钩子按 args 判定高风险 → **deny**
            → 引擎 emit skill_dispatch_hook_denied，子 turn 不执行
       └─ turn 2：改用 call_skill data-export {"scope":"recent"}
            → 钩子放行 → 子 turn 执行 → post_skill_dispatch 审计（仅记录）
       └─ turn 3：综合回复

钩子 vs 权限规则（本 demo 的核心对照）：
    - 权限规则（PermissionPolicy）：静态、可序列化、按 skill_id / pattern 匹配。
      表达不了「同一个 data-export，scope=all 拒、scope=recent 放」。
    - 钩子（HookRunner）：进程内 async 回调，能读 ``hook.args`` 做按入参的动态决策。
    本 demo 正是用 pre_skill_dispatch 钩子读 args.scope 实现动态分流。

可视化（attach_console_sink）：
    [SKILL DISP?] 第一次派发 data-export（scope=all）
    [HOOK DENY ]  skill_dispatch_hook_denied —— 钩子按 args 拦截
    [SKILL DISP]  第二次派发 data-export（scope=recent）放行
    [SKILL RET ]  子 turn 结果回流
    [LLM FINAL ]  task-runner 综合回复

运行（SimClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/hooks_showcase/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from hooks_lib import build_showcase_hook_runner

import taifeng
from taifeng.llm.providers.sim import SimTurn, RoutingSimClient
from taifeng.telemetry import attach_console_sink

SKILLS_DIR = Path(__file__).parent / "skills"


def _routing_client() -> RoutingSimClient:
    """RoutingSimClient：

    - task-runner 首轮派发 scope=all（会被钩子 deny），次轮改 scope=recent（放行），三轮综合；
    - data-export 仅在 scope=recent 时真正执行一次（scope=all 在 pre 钩子被拦，不进子 turn）。

    按各 skill body 内的唯一标记路由，回放与派发顺序无关。
    """
    return RoutingSimClient(routes={
        "HOOKS_TASK_RUNNER_MARK": [
            SimTurn(text="先尝试全量导出。", tool_calls=[
                {"id": "h0", "name": "call_skill",
                 "arguments": '{"skill_id": "data-export", '
                              '"args": {"scope": "all"}, "reason": "全量导出"}'},
            ]),
            SimTurn(text="全量被业务钩子拒，改用近期数据导出。", tool_calls=[
                {"id": "h1", "name": "call_skill",
                 "arguments": '{"skill_id": "data-export", '
                              '"args": {"scope": "recent"}, "reason": "近期导出"}'},
            ]),
            SimTurn(text="全量导出被风控钩子拦截；已改用近期数据完成导出。"),
        ],
        "HOOKS_DATA_EXPORT_MARK": [
            SimTurn(text="导出完成：近期 120 条记录。"),
        ],
    })


async def main() -> None:
    """跑一次 task-runner，控制台打印事件流（观察钩子按 args 拦截再放行）。"""
    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=threads,
            model_client=_routing_client(),
            compressors=[],
            # 注入业务钩子：pre_skill_dispatch（可否决）+ post_skill_dispatch（审计）
            hooks=build_showcase_hook_runner(),
        )
        engine = await pool.get_or_create(
            session_id="demo-hooks",
            entry_skill_id="task-runner",
        )
        sink_task = attach_console_sink(engine, color=True)

        sub_id = await engine.submit(taifeng.UserMessage(
            text="请帮我导出数据，优先全量；若被风控拦截则退而求其次导出近期数据。",
        ))
        async for ev in engine.subscribe(sub_id):
            done = ev.msg.kind in ("turn_completed", "turn_failed")
            if done and ev.msg.data.get("is_root"):
                break

        # SimClient 瞬时完成，给异步 console_sink 时间把事件打印完整再收尾
        await asyncio.sleep(0.5)
        await pool.close()
        await asyncio.sleep(0.2)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
