"""orchestration-suspension-propagation 端到端 —— 编排路径挂起传递 + 重入重放。

- 子 skill 挂起 → 编排 turn 正确挂起(CHILD_SKILL 上浮),占位文本不入史,后续段不执行
- Resume(leaf) → 嵌套续跑链回填 → 重入按 call_id 重放已完成子(零派发)→ 后续段执行
- 混合批:完成子配对入史 / 挂起子悬空 fc
- when 段判定由重放恢复
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.submission import Resume
from taifeng.suspend.reason import SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

if TYPE_CHECKING:
    from pathlib import Path


def _write(skills: Path, name: str, body: str) -> None:
    d = skills / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def _entry(orch_yaml: str, children: list[str]) -> str:
    return (
        "---\n"
        "name: flow\ndescription: 编排入口\nversion: 1.0.0\n"
        "type: composite\nentry: true\nmodel: mock-model\n"
        f"child_skills: [{', '.join(children)}]\ntool_names: []\nmax_call_depth: 3\n"
        f"{orch_yaml}"
        "---\n# 编排 FLOW_MARK\n"
    )


_ASKER = """---
name: asker
description: 问询子技能
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 ASKER_MARK
先问人。
"""

_PLAIN = (
    "---\n"
    "name: plain\ndescription: 普通子技能\nversion: 1.0.0\ntype: atomic\n"
    "---\n# 普通 PLAIN_MARK\n直接出结论。\n"
)

_CLOSER = (
    "---\n"
    "name: closer\ndescription: 收尾子技能\nversion: 1.0.0\ntype: atomic\n"
    "---\n# 收尾 CLOSER_MARK\n汇总收尾。\n"
)


async def _watch_all(engine, events: list):
    async for ev in engine.subscribe_all():
        events.append(ev.msg)
        if ev.msg.kind == "shutdown":
            return


async def _wait(cond, tries: int = 200) -> bool:
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def _root_suspended(events: list, root_tid: str):
    for m in events:
        if m.kind == "turn_suspended" and m.data["thread_id"] == root_tid:
            return m.data
    return None


@pytest.mark.asyncio
async def test_orch_suspend_resume_replay_e2e(tmp_path: Path, threads_dir: Path) -> None:
    """两段 serial:[plain] → [asker]。asker 挂起 → 编排 turn 挂起(占位文本不入史、
    plain 的配对保留);Resume(leaf 补答)→ 重入重放 plain(零派发)→ asker 续跑 →
    编排完成。覆盖:挂起分流 / CHILD_SKILL 上浮 / call_id 重放 / 嵌套续跑链。"""
    skills = tmp_path / "s"
    _write(skills, "flow", _entry(
        "orchestration:\n  steps:\n    - serial: [plain]\n    - serial: [asker]\n",
        ["plain", "asker"],
    ))
    _write(skills, "asker", _ASKER)
    _write(skills, "plain", _PLAIN)
    client = RoutingMockClient(routes={
        # plain 只有 1 个脚本:若重入被重派发,第二次会拿到空文本(脚本耗尽)→ 断言可抓
        "PLAIN_MARK": [MockTurn(text="PLAIN_DONE")],
        "ASKER_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "aq1", "name": "request_user_input",
                 "arguments": '{"prompt": "请补充"}'}]),
            MockTurn(text="ASKER_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="orch-sus", entry_skill_id="flow")
    events: list = []
    task = asyncio.create_task(_watch_all(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="开始"))
    assert await _wait(lambda: _root_suspended(events, engine.thread_id) is not None), \
        "编排 turn 应以挂起终结(而非吞掉子挂起继续跑)"
    suspended = _root_suspended(events, engine.thread_id)

    # 根 pending 是 CHILD_SKILL(call_skill 上浮),leaf 在 detail.sub_thread_id
    root_pending = suspended["pending"][0]
    assert root_pending["reason"] == SuspendReason.CHILD_SKILL.value
    leaf_tid = root_pending["detail"]["sub_thread_id"]

    # 占位文本不得入史;plain 的配对已落史;asker 的 fc 悬空
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    all_outputs = [str(it.payload.get("output", "")) for it in items
                   if it.kind == "function_call_output"]
    assert not any("<suspended>" in o for o in all_outputs), \
        "占位文本入史 = silent fallback 复发"
    plain_fco = [o for o in all_outputs if "PLAIN_DONE" in o]
    assert plain_fco, "完成子 plain 的配对输出应已入史"

    # leaf 真实挂起:DATA(request_user_input)
    leaf_items = [it async for it in await pool.store.load_thread(leaf_tid)]
    recs = [it for it in leaf_items if it.kind == "suspension"]
    leaf_rec = SuspensionRecord.from_item(recs[-1])
    assert leaf_rec.pending[0].reason is SuspendReason.DATA

    # Resume(leaf 补答) → 嵌套链回填 → 重入重放 → 编排完成
    await engine.submit(Resume(
        thread_id=leaf_tid,
        resolutions={leaf_rec.pending[0].request_id: {"answer": "好的"}},
    ))

    def _root_completed():
        for m in events:
            if m.kind == "turn_completed" and m.data.get("is_root"):
                return m.data
        return None

    assert await _wait(lambda: _root_completed() is not None), "重入后编排应完成"
    assert _root_completed()["end_reason"] == "completed"

    # 重放验证:plain 只被采样一次(脚本未耗尽 → 史上无空输出的二次配对)
    items2 = [it async for it in await pool.store.load_thread(engine.thread_id)]
    plain_fcos = [it for it in items2 if it.kind == "function_call_output"
                  and "plain" in str(it.payload.get("call_id", ""))]
    assert len(plain_fcos) == 1, \
        f"重入应重放 plain 而非重派发,实有 {len(plain_fcos)} 个配对"
    # 重放命中可观测:重入批的 tool_batch_dispatched 至少出现一次 count=0 或 1
    counts = [m.data["count"] for m in events if m.kind == "tool_batch_dispatched"]
    assert 0 in counts, f"重入首段应零派发(重放命中),实得 {counts}"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


@pytest.mark.asyncio
async def test_orch_mixed_parallel_batch(tmp_path: Path, threads_dir: Path) -> None:
    """parallel:[plain, asker] 混合批:plain 配对入史、asker 悬空 fc、
    record 含 1 个 pending、turn suspended;Resume 后 plain 重放零重跑、整体完成。"""
    skills = tmp_path / "s"
    _write(skills, "flow", _entry(
        "orchestration:\n  steps:\n    - parallel: [plain, asker]\n",
        ["plain", "asker"],
    ))
    _write(skills, "asker", _ASKER)
    _write(skills, "plain", _PLAIN)
    client = RoutingMockClient(routes={
        "PLAIN_MARK": [MockTurn(text="PLAIN_DONE")],
        "ASKER_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "aq2", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            MockTurn(text="ASKER_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
        max_parallel_tool_calls=2,
    )
    engine = await pool.get_or_create(session_id="orch-mix", entry_skill_id="flow")
    events: list = []
    task = asyncio.create_task(_watch_all(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="开始"))
    assert await _wait(lambda: _root_suspended(events, engine.thread_id) is not None)
    suspended = _root_suspended(events, engine.thread_id)
    assert len(suspended["pending"]) == 1, "只有 asker 一个挂起子"

    # 混合批历史形状:plain (fc,fco) 配对;asker fc 悬空(无 fco)
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    fc_ids = [str(it.payload.get("call_id")) for it in items
              if it.kind == "function_call"]
    fco_ids = [str(it.payload.get("call_id")) for it in items
               if it.kind == "function_call_output"]
    asker_fc = [c for c in fc_ids if "asker" in c]
    assert asker_fc and asker_fc[0] not in fco_ids, "挂起子应留悬空 fc"
    plain_fc = [c for c in fc_ids if "plain" in c]
    assert plain_fc and plain_fc[0] in fco_ids, "完成子应配对完整"

    # 续跑至完成
    leaf_tid = suspended["pending"][0]["detail"]["sub_thread_id"]
    leaf_items = [it async for it in await pool.store.load_thread(leaf_tid)]
    leaf_rec = SuspensionRecord.from_item(
        [it for it in leaf_items if it.kind == "suspension"][-1])
    await engine.submit(Resume(
        thread_id=leaf_tid,
        resolutions={leaf_rec.pending[0].request_id: {"answer": "ok"}},
    ))
    assert await _wait(lambda: any(
        m.kind == "turn_completed" and m.data.get("is_root") for m in events))
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


@pytest.mark.asyncio
async def test_orch_when_branch_replay_consistent(
    tmp_path: Path, threads_dir: Path
) -> None:
    """picker 出 flag → when 选 then 段(asker)挂起;重入后 picker 重放,
    when 判定与挂起前一致(then 段续跑),else 段(closer)不被执行。"""
    skills = tmp_path / "s"
    _write(skills, "flow", _entry(
        "orchestration:\n  steps:\n"
        "    - serial: [picker]\n"
        "    - when:\n"
        "        condition: go\n"
        "        then: [asker]\n"
        "        else: [closer]\n",
        ["picker", "asker", "closer"],
    ))
    _write(skills, "picker", (
        "---\n"
        "name: picker\ndescription: 选路\nversion: 1.0.0\ntype: atomic\n"
        "---\n# 选路 PICKER_MARK\n输出 flag。\n"
    ))
    _write(skills, "asker", _ASKER)
    _write(skills, "closer", _CLOSER)
    client = RoutingMockClient(routes={
        "PICKER_MARK": [MockTurn(text='{"go": true}')],
        "ASKER_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "aq3", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            MockTurn(text="ASKER_DONE"),
        ],
        "CLOSER_MARK": [MockTurn(text="CLOSER_SHOULD_NOT_RUN")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="orch-when", entry_skill_id="flow")
    events: list = []
    task = asyncio.create_task(_watch_all(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="开始"))
    assert await _wait(lambda: _root_suspended(events, engine.thread_id) is not None)
    suspended = _root_suspended(events, engine.thread_id)
    leaf_tid = suspended["pending"][0]["detail"]["sub_thread_id"]
    leaf_items = [it async for it in await pool.store.load_thread(leaf_tid)]
    leaf_rec = SuspensionRecord.from_item(
        [it for it in leaf_items if it.kind == "suspension"][-1])
    await engine.submit(Resume(
        thread_id=leaf_tid,
        resolutions={leaf_rec.pending[0].request_id: {"answer": "ok"}},
    ))
    assert await _wait(lambda: any(
        m.kind == "turn_completed" and m.data.get("is_root") for m in events))

    # when 重放一致:closer(else 段)从未执行
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    all_fc = [str(it.payload.get("call_id")) for it in items
              if it.kind == "function_call"]
    assert not any("closer" in c for c in all_fc), \
        "重入后 when 判定应与挂起前一致(then 段),else 段不得执行"
    assert sum(1 for c in all_fc if "picker" in c) == 1, "picker 重放不重跑"
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
