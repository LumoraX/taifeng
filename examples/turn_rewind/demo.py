"""turn-rewind 体验 demo —— 自治链「一键跑完」+ 回退到任意节点重跑(纯 SimClient)。

演示内核的 **turn-rewind** 能力:把一次 root turn 拆成可寻址的**回访节点表**,
业务侧可对任意节点直接 retry —— 子 skill 全程 ``entry: false``,绕开 entry/call_skill 互斥。

两个场景:
  A. ``retry_tool`` 重跑一次 ``call_skill`` —— 保留 LLM「决定调它」的动作,只把那个
     子 skill 重跑一遍、换掉它的输出,父 skill 基于新结论续推(头号场景:重跑自治 run
     里的中间某一步)。
  B. ``re_reason`` 回退到某圈 LLM 采样前 —— 截断后让 LLM 从该点**重新决定**下游。

运行(无需 API key):
    cd taifeng
    PYTHONPATH=src python examples/turn_rewind/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.submission import Rewind

# ── skills 目录：磁盘单一真相，orchestrator(entry composite)+ analyzer(子 skill,entry:false)──
# 不再用内联字符串常量，由 examples/turn_rewind/skills/ 目录直接提供 SKILL.md 文件。
SKILLS_DIR = Path(__file__).parent / "skills"

CALL_ANALYZER = (
    '{"skill_id":"analyzer","reason":"取专科分析结论",'
    '"args":{"patient":"病例X"}}'
)


async def _drive(engine: taifeng.AgentEngine, sub_id: str) -> str:
    """消费某 submission 的事件到 **root turn** 终结,返回拼接的 assistant 文本。

    必须走 subscribe_all + is_root 过滤:``subscribe(sub_id)`` 会在 call_skill 子 turn
    的 turn_completed(is_root=False)处提前断流(见 engine.subscribe 终止集合)。
    """
    parts: list[str] = []
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        data = ev.msg.data if hasattr(ev.msg, "data") else {}
        if ev.msg.kind == "assistant_text" and data.get("delta"):
            parts.append(str(data["delta"]))
        if ev.msg.kind in ("turn_completed", "turn_failed") and data.get("is_root"):
            break
    return "".join(parts)


def _print_nodes(engine: taifeng.AgentEngine) -> None:
    """打印当前 root turn 的回访节点表。"""
    print("  回访节点表 engine.rewind_nodes():")
    for n in engine.rewind_nodes():
        tail = f"  target={n.target_id}" if n.kind == "dispatch" else ""
        print(f"    - {n.node_id:6} kind={n.kind:9} iter={n.iteration_index}{tail}")


async def _settle_nodes(engine: taifeng.AgentEngine) -> None:
    """turn_completed 在 run() 内发出、状态回写在 run() 返回后;轮询等节点表落地。"""
    for _ in range(100):
        if engine.rewind_nodes():
            return
        await asyncio.sleep(0.01)


async def scenario_retry_tool() -> None:
    """A. retry_tool 重跑一次 call_skill —— 子 skill 真重跑、父基于新结论续推。"""
    print("\n=== 场景 A：retry_tool 重跑自治链里的一次 call_skill ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # skills 来自磁盘固定目录，临时目录仅用于 threads_dir（会话存储）
        client = SimClient(turns=[
            # 一键跑完:orchestrator → call_skill(analyzer) → 综合
            SimTurn(text="先派发专科分析…", tool_calls=[
                {"id": "a0", "name": "call_skill", "arguments": CALL_ANALYZER}]),
            SimTurn(text="【分析】风险偏高(初版)"),
            SimTurn(text="【综合】建议:加强监测(基于初版)"),
            # —— retry_tool 后:analyzer 重跑 + orchestrator 续推 ——
            SimTurn(text="【分析】风险中等(修订版)"),
            SimTurn(text="【综合】建议:常规随访(基于修订版)"),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR, threads_dir=root / "threads",
            model_client=client, compressors=[],
        )
        engine = await pool.get_or_create(
            session_id="rw-a", entry_skill_id="orchestrator")

        sub = await engine.submit(taifeng.UserMessage(text="分析病例 X"))
        print("  [一键跑完] →", await _drive(engine, sub))
        await _settle_nodes(engine)
        _print_nodes(engine)

        # 取那次 call_skill 的 dispatch 节点,retry_tool 重跑它
        disp = next(n for n in engine.rewind_nodes()
                    if n.kind == "dispatch" and n.target_id == "call_skill")
        print(f"\n  → Rewind({disp.node_id}, mode=retry_tool):保留「决定调 analyzer」,"
              "只重跑 analyzer")
        rsub = await engine.submit(
            Rewind(node_id=disp.node_id, mode="retry_tool"))
        print("  [retry_tool 后] →", await _drive(engine, rsub))
        print("  ↑ analyzer 走了新子 turn(风险中等),orchestrator 基于修订版重新综合")
        await pool.close()


async def scenario_re_reason() -> None:
    """B. re_reason 回退到某圈采样前 —— LLM 重新决定下游。"""
    print("\n=== 场景 B：re_reason 回退到某圈 LLM 采样前,让它重新决定 ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # skills 来自磁盘固定目录，临时目录仅用于 threads_dir（会话存储）
        client = SimClient(turns=[
            SimTurn(text="先派发专科分析…", tool_calls=[
                {"id": "a0", "name": "call_skill", "arguments": CALL_ANALYZER}]),
            SimTurn(text="【分析】风险偏高"),
            SimTurn(text="【综合】建议:加强监测"),
            # —— re_reason 到 it1 后:LLM 重决,这次直接不派发、给保守结论 ——
            SimTurn(text="【综合】重新判断:信息不足,先补检查再说"),
        ])
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR, threads_dir=root / "threads",
            model_client=client, compressors=[],
        )
        engine = await pool.get_or_create(
            session_id="rw-b", entry_skill_id="orchestrator")

        sub = await engine.submit(taifeng.UserMessage(text="分析病例 X"))
        print("  [一键跑完] →", await _drive(engine, sub))
        await _settle_nodes(engine)
        _print_nodes(engine)

        print("\n  → Rewind(it1, mode=re_reason):截到第 1 圈采样前,LLM 重新决定下游")
        rsub = await engine.submit(Rewind(node_id="it1", mode="re_reason"))
        print("  [re_reason 后] →", await _drive(engine, rsub))
        print("  ↑ 这次 LLM 没派发 analyzer,直接走出保守结论(下游自适应)")
        await pool.close()


async def main() -> None:
    await scenario_retry_tool()
    await scenario_re_reason()
    print("\n🎉 turn-rewind:自治一键跑完 + 回退任意节点重跑(retry_tool / re_reason)演示完毕")


if __name__ == "__main__":
    asyncio.run(main())
