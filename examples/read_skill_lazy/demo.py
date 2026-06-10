"""read_skill 懒加载 demo —— skill-as-context 范式核心。

场景：

    knowledge-router (composite entry)
       └─ turn 1：LLM 判断需要 SQL 指南 → read_skill(sql-injection-guide)
            （只把该 skill 的 body 注入上下文，**不派发子 turn**）
       └─ turn 2：拿到指南正文后据此作答

要点：子 skill 的完整 body **不预先进 prompt**（只给 id + 描述，见
`<available_child_skills>`）；LLM 用 read_skill **按需取** body → 上下文只装当前
需要的知识，省 token。这是 taifeng「skill 是 markdown、LLM 是调度器」范式的基石，
与 call_skill（派发子 turn 隔离执行）互补。

可视化（attach_console_sink）：
    [TOOL CALL]   read_skill{skill_id: sql-injection-guide}
    [TOOL RET]    返回该 skill 的 body（注入上下文）
    [LLM FINAL]   基于刚读入的指南作答

运行（SimClient，**无需 API key**）：

    cd taifeng
    PYTHONPATH=src uv run python examples/read_skill_lazy/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.telemetry import attach_console_sink

SKILLS_DIR = Path(__file__).parent / "skills"


def _client() -> SimClient:
    """两轮：turn1 调 read_skill 取 SQL 指南；turn2 基于读入的 body 作答。

    只有 entry（knowledge-router）采样——子 skill 是被「读」而非被「调」，不起子 turn，
    故用最简单的顺序 SimClient 即可。
    """
    return SimClient(turns=[
        SimTurn(
            text="这道题关于 SQL 注入，先按需读取相关指南。",
            tool_calls=[{
                "id": "rs0",
                "name": "read_skill",
                "arguments": '{"skill_id": "sql-injection-guide"}',
            }],
        ),
        SimTurn(
            text=(
                "根据刚读入的《SQL 注入防护指南》：核心是永不拼接用户输入——"
                "用参数化查询 / 预编译语句，配合最小权限账号与输入校验。"
            ),
        ),
    ])


async def main() -> None:
    """跑一次 knowledge-router，控制台打印 read_skill 懒加载流。"""
    with tempfile.TemporaryDirectory() as td:
        threads = Path(td) / "threads"
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=threads,
            model_client=_client(),
            compressors=[],
        )
        engine = await pool.get_or_create(
            session_id="demo-read-skill",
            entry_skill_id="knowledge-router",
        )
        sink_task = attach_console_sink(engine, color=True)

        sub_id = await engine.submit(taifeng.UserMessage(
            text="我的登录接口怎么防 SQL 注入？",
        ))
        async for ev in engine.subscribe(sub_id):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                break

        await asyncio.sleep(0.3)
        await pool.close()
        await asyncio.sleep(0.1)
        sink_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
