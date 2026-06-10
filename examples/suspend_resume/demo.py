"""挂起 / Resume 头条故事 —— 表单采集型 HITL 跨实例续跑（纯 SimClient，无需 API key）。

讲一个端到端的故事（每步打印清晰旁白）：

  1. 起一个 engine（经 EnginePool），entry skill 由 LLM 驱动去调用
     ``request_user_input`` 工具（表单 / 数据采集型 HITL）。
  2. 提交 UserMessage → turn 在 ``request_user_input`` 处 **挂起**：
     ``engine.subscribe(sub_id)`` 循环在 ``turn_suspended`` 事件上 **自然结束**
     （此刻实例已空闲 —— 可以释放它数小时）。打印 record_id 与待回答的
     prompt + response_schema。
  3. **彻底释放 / 销毁** engine + pool（模拟进程退出 / 实例回收）。
  4. 从持久化 store **重建**：为 **同一个 thread_id** 新建 pool / engine
     （resume-by-thread-id），证明挂起状态在 JSONL store 中存活。
  5. 向重建后的 engine 提交 ``Resume(thread_id, {request_id: <表单答案>})``：
     续跑的 turn 完成 → subscribe 在 ``turn_completed`` 结束。打印最终
     assistant 文本，并展示 transcript 现在补回了 ``function_call_output``（gap 已填）。

运行：

    cd taifeng
    PYTHONPATH=src uv run python examples/suspend_resume/demo.py

参照 tests/test_suspend.py::test_tier2_rebuild_resume（跨实例重建 resume）与
test_request_user_input_raises_data_suspend（DATA 挂起原语），把测试里的可工作
搭建改写成线性可读的 demo —— 把 pytest 断言换成 print 旁白。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

# 一个最小的 atomic 子 skill：composite skill 必须声明非空 child_skills 才能通过校验。
CHILD_SKILL = """---
name: vitals-checker
description: 体征指标核对
version: 1.0.0
type: atomic
---
# 体征核对
按年龄段核对各项体征指标是否正常。
"""

# entry composite skill：声明 tool_names 含 request_user_input，否则会被
# turn.py 的工具白名单过滤掉。body 不决定何时调工具 —— 那由 SimClient 脚本决定。
ENTRY_SKILL = """---
name: intake-assistant
description: 问诊信息采集助手
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [vitals-checker]
tool_names: [request_user_input]
max_call_depth: 2
---
# 信息采集助手

你负责在分析前补齐缺失信息。缺信息时调用 `request_user_input(prompt, response_schema)`
向用户发问；拿到回答后给出最终结论。
"""

# 这条 call_id 在两个 SimClient 脚本里都要用到（request_user_input 把它同时
# 当作 PendingRequest.request_id 与 related_call_id —— resume 回填的锚点）。
FORM_CALL_ID = "call_intake_1"


def _build_skill(skills_dir: Path) -> None:
    """把 entry skill 写入磁盘（重建实例时直接复用同一份）。"""
    (skills_dir / "vitals-checker").mkdir(parents=True)
    (skills_dir / "vitals-checker" / "SKILL.md").write_text(
        CHILD_SKILL, encoding="utf-8",
    )
    (skills_dir / "intake-assistant").mkdir(parents=True)
    (skills_dir / "intake-assistant" / "SKILL.md").write_text(
        ENTRY_SKILL, encoding="utf-8",
    )


def _suspending_client() -> SimClient:
    """实例#1 的脚本：第一轮产出一个 request_user_input 调用（命中表单挂起）。

    挂起后 turn 不再采样，所以这里只需要一轮。
    """
    return SimClient(turns=[
        SimTurn(
            text="为完成评估，我需要先采集您的年龄。",
            tool_calls=[{
                "id": FORM_CALL_ID,
                "name": "request_user_input",
                "arguments": (
                    '{"prompt": "请问您的年龄是多少？", '
                    '"response_schema": {"type": "object", '
                    '"properties": {"age": {"type": "integer"}}, '
                    '"required": ["age"]}}'
                ),
            }],
            usage=TokenUsage(input_tokens=120, output_tokens=20),
        ),
    ])


def _resuming_client() -> SimClient:
    """实例#2 的脚本：resume 续跑只需一轮成功完成（纯文本，无新工具调用）。"""
    return SimClient(turns=[
        SimTurn(
            text="收到，您今年 35 岁。结论：各项指标在该年龄段属正常范围。",
            usage=TokenUsage(input_tokens=90, output_tokens=24),
        ),
    ])


async def _drain_until_terminal(engine: taifeng.AgentEngine, sub_id: str) -> list:
    """订阅某 submission 直到终结事件（completed / failed / suspended），返回事件列表。

    挂起 turn 改发独立终结态 turn_suspended（不再发 turn_completed），所以
    break 条件必须纳入 turn_suspended，否则消费者会卡死在订阅循环。
    """
    events = []
    async for ev in engine.subscribe(sub_id):
        events.append(ev)
        if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
            break
    return events


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills_dir = root / "skills"
        # threads_dir 是跨实例存活的关键：两个 pool 共用它，挂起状态才能续接
        threads_dir = root / "threads"
        threads_dir.mkdir(parents=True)
        _build_skill(skills_dir)

        # ============================================================
        # 步骤 1：起实例#1，提交消息触发挂起
        # ============================================================
        print("=" * 64)
        print("步骤 1/5：构建实例#1（pool + engine），提交一条用户消息")
        print("=" * 64)

        pool1 = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=threads_dir,
            model_client=_suspending_client(),
            compressors=[],                          # 演示极简，关闭压缩
            extra_tools=[make_request_user_input_tool()],  # opt-in 注册采集工具
        )
        engine1 = await pool1.get_or_create(
            session_id="intake-session", entry_skill_id="intake-assistant",
        )
        thread_id = engine1.thread_id
        print(f"  engine#1 已就绪  thread_id={thread_id}")

        sub_id = await engine1.submit(taifeng.UserMessage(text="帮我做个健康评估"))
        print("  已提交 UserMessage：'帮我做个健康评估'\n")

        # ============================================================
        # 步骤 2：turn 在 request_user_input 处挂起，subscribe 自然结束
        # ============================================================
        print("=" * 64)
        print("步骤 2/5：turn 命中 request_user_input → 挂起；subscribe 自然结束")
        print("=" * 64)

        events1 = await _drain_until_terminal(engine1, sub_id)
        suspended = events1[-1]
        assert suspended.msg.kind == "turn_suspended", (
            f"应在 turn_suspended 处结束，实得 {suspended.msg.kind}"
        )
        print("  subscribe 循环已在 turn_suspended 事件上自然结束 ——")
        print("  实例此刻已空闲，我们完全可以把它释放掉数小时。\n")

        record_id = suspended.msg.data["record_id"]
        pending = suspended.msg.data["pending"]
        print(f"  turn_suspended.record_id = {record_id}")
        print(f"  cache_invalidated        = {suspended.msg.data['cache_invalidated']}")
        print(f"  待回答的 prompt           = {pending[0]['detail']['prompt']}")
        print(f"  期望的 response_schema    = {pending[0]['detail']['response_schema']}\n")

        # 从持久化 store 取回 record，拿到续跑入参 request_id
        items1 = [it async for it in await pool1.store.load_thread(thread_id)]
        rec = SuspensionRecord.from_item(
            next(it for it in items1 if it.kind == "suspension")
        )
        req_id = rec.pending[0].request_id
        # history-gap 自检：function_call 已落盘，但还没有配对的 function_call_output
        fc_ids = {it.payload.get("call_id") for it in items1 if it.kind == "function_call"}
        fco_ids = {it.payload.get("call_id") for it in items1 if it.kind == "function_call_output"}
        print(f"  落盘 request_id = {req_id}（resume 入参的 key）")
        print(f"  history-gap：function_call 已落盘={FORM_CALL_ID in fc_ids}，"
              f"function_call_output 尚缺={FORM_CALL_ID not in fco_ids}\n")

        # ============================================================
        # 步骤 3：彻底释放实例#1（模拟进程退出 / 实例回收）
        # ============================================================
        print("=" * 64)
        print("步骤 3/5：彻底释放实例#1（模拟进程退出 / 实例回收）")
        print("=" * 64)
        await pool1.close()
        del pool1, engine1
        print("  pool#1 已 close，引用已删除。内存中不再有任何活跃实例。")
        print(f"  但挂起状态仍躺在磁盘 JSONL store：{threads_dir}\n")

        # ============================================================
        # 步骤 4：用同一个 thread_id 重建实例#2（resume-by-thread-id）
        # ============================================================
        print("=" * 64)
        print("步骤 4/5：从持久化 store 重建实例#2（同 thread_id）")
        print("=" * 64)
        pool2 = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=threads_dir,                 # 同一个 threads_dir
            model_client=_resuming_client(),
            compressors=[],
            extra_tools=[make_request_user_input_tool()],
        )
        # 全新 session_id + resume_thread_id：续接同一持久化 thread
        engine2 = await pool2.get_or_create(
            session_id="intake-rebuilt",
            entry_skill_id="intake-assistant",
            resume_thread_id=thread_id,
        )
        assert engine2.thread_id == thread_id, "重建 engine 必须复用原 thread_id"
        print(f"  engine#2 已就绪并复用原 thread_id={engine2.thread_id}")
        print("  挂起状态跨实例存活 —— 证明 R5 可 resume。\n")

        # ============================================================
        # 步骤 5：提交 Resume（带表单答案）→ 续跑完成
        # ============================================================
        print("=" * 64)
        print("步骤 5/5：向重建实例提交 Resume（携带表单答案）→ 续跑完成")
        print("=" * 64)
        # 表单答案直接进 resolutions[req_id]，由 resolver 回填成该 call 的
        # function_call_output（gap 补齐）
        answer = {"age": 35}
        print(f"  提交 Resume(thread_id={thread_id}, resolutions={{{req_id!r}: {answer}}})")
        resume_sub = await engine2.submit(taifeng.Resume(
            thread_id=thread_id,
            resolutions={req_id: answer},
        ))
        events2 = await _drain_until_terminal(engine2, resume_sub)

        items2 = [it async for it in await pool2.store.load_thread(thread_id)]
        await pool2.close()

        kinds2 = [ev.msg.kind for ev in events2]
        assert "suspension_resolved" in kinds2, f"未见 suspension_resolved，实得 {kinds2}"
        assert events2[-1].msg.kind == "turn_completed", f"续跑应完成，实得 {kinds2}"
        print(f"  事件序列：{kinds2}")

        # gap 补齐自检：被挂起的 call 现在有了 function_call_output
        fco_ids2 = {it.payload.get("call_id") for it in items2
                    if it.kind == "function_call_output"}
        print(f"  gap 已补齐：{FORM_CALL_ID} 现在有了 function_call_output="
              f"{FORM_CALL_ID in fco_ids2}")

        # 取最终 assistant 文本
        final_text = next(
            (it.payload.get("text", "") for it in reversed(items2)
             if it.kind == "assistant_message"),
            "",
        )
        print(f"\n  最终 assistant 文本：{final_text}")
        print("\n" + "=" * 64)
        print("故事完结：挂起 → 释放实例 → 跨实例重建 → Resume → 续跑完成 ✓")
        print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
