"""budget-awareness 端到端 —— pre-turn 用量穿越 soft_limit 注中性预算事实。

验证 ADR 0017 规则②原语接进主循环：history 用量过 soft → 注一条
``system_injection``(source="budget_hint") + emit ``budget_hint_injected``；
不过 soft → 零注入零事件。
"""
from __future__ import annotations

import asyncio

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.llm.providers import SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.tool.spec import ToolResult, ToolSpec


def _noop_tool() -> ToolSpec:
    """极简 no-op 工具：仅用于满足 composite entry 的能力声明（模型不会调用它）。"""

    async def _handler(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True)


async def _wait(cond, tries: int = 300) -> bool:
    """轮询等待条件成立（每次 10ms）。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _entry_skill(root):
    """写一个极简 composite entry skill（body 含 ENTRY_MARK）。"""
    d = root / "entry"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: entry\ndescription: entry\nversion: 1.0.0\n"
        "type: composite\nentry: true\nmodel: mock-model\n"
        "tool_names: [echo]\nmax_call_depth: 2\n"
        "---\n# entry ENTRY_MARK\n",
        encoding="utf-8")
    return root


def _collect(engine):
    """submit 前启动 subscribe_all 收集器。"""
    events: list = []

    async def _run():
        async for ev in engine.subscribe_all():
            events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    return events, asyncio.create_task(_run())


# soft=0.5*4000=2000，hard=0.95*4000=3800：让 ~2285 token 的用户消息过 soft、不过 hard
def _small_budget() -> ContextBudget:
    return ContextBudget(context_window=4000, soft_limit_ratio=0.5, hard_limit_ratio=0.95)


@pytest.mark.asyncio
async def test_budget_hint_injected_on_soft_crossing(tmp_path, threads_dir):
    """history 用量过 soft → emit budget_hint_injected + history 落 budget_hint 注入项。"""
    skills = _entry_skill(tmp_path / "bh")
    client = RoutingSimClient(routes={"ENTRY_MARK": [SimTurn(text="DONE")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], budget=_small_budget(), extra_tools=[_noop_tool()])
    engine = await pool.get_or_create(session_id="bh", entry_skill_id="entry")
    events, task = _collect(engine)
    await asyncio.sleep(0)

    # ~8000 字符 → ~2285 token，过 soft(2000) 不过 hard(3800)
    big = "x" * 8000
    sub_id = await engine.submit(taifeng.UserMessage(text=big))
    assert await _wait(
        lambda: any(ev.msg.kind == "turn_completed"
                    and ev.submission_id == sub_id
                    and ev.msg.data.get("is_root") for ev in events)), "turn 未完成"

    hint = [ev for ev in events if ev.msg.kind == "budget_hint_injected"]
    assert hint, f"过 soft 应 emit budget_hint_injected，实得 {[e.msg.kind for e in events]}"
    data = hint[0].msg.data
    assert data["used"] >= 2000, f"used 应 ≥ soft，实得 {data['used']}"
    assert data["ratio"] >= 0.5
    assert data["remaining_to_hard"] == max(0, 3800 - data["used"])

    # history 落一条 source=budget_hint 的 system_injection，且文本是中性事实
    items = engine.history_snapshot()
    notes = [it for it in items
             if it.kind == "system_injection"
             and it.payload.get("source") == "budget_hint"]
    assert len(notes) == 1, f"应恰一条 budget_hint 注入项，实得 {len(notes)}"
    assert "%" in notes[0].payload["text"], "注入文本应含百分比事实"

    task.cancel()
    await pool.close()


@pytest.mark.asyncio
async def test_no_budget_hint_when_under_soft(tmp_path, threads_dir):
    """history 用量不过 soft → 零注入零事件（穿越一次注一次的「穿越」未发生）。"""
    skills = _entry_skill(tmp_path / "bh2")
    client = RoutingSimClient(routes={"ENTRY_MARK": [SimTurn(text="DONE")]})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], budget=_small_budget(), extra_tools=[_noop_tool()])
    engine = await pool.get_or_create(session_id="bh2", entry_skill_id="entry")
    events, task = _collect(engine)
    await asyncio.sleep(0)

    sub_id = await engine.submit(taifeng.UserMessage(text="hi"))
    assert await _wait(
        lambda: any(ev.msg.kind == "turn_completed"
                    and ev.submission_id == sub_id
                    and ev.msg.data.get("is_root") for ev in events)), "turn 未完成"

    assert not [ev for ev in events if ev.msg.kind == "budget_hint_injected"], \
        "未过 soft 不应 emit budget_hint_injected"
    items = engine.history_snapshot()
    assert not [it for it in items
                if it.kind == "system_injection"
                and it.payload.get("source") == "budget_hint"], "未过 soft 不应有注入项"

    task.cancel()
    await pool.close()
