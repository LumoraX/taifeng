"""resume 后 history → OpenAI messages 重建的合法性回归。

复现并固化两个缺陷（医生端 OpenAI-compat 代理迁移顶出的边界）：

- Defect 1（call_id 错配）：call_skill 派发的子 skill 挂起后，父 call_skill 的
  ``function_call`` 用 LLM 给的 call_id 落盘，但 resume 回填的 ``function_call_output``
  误用了子帧内部 id（``sk_*``）→ assistant(tool_calls=[CS]) 无匹配 tool + orphan tool
  → OpenAI-compat 代理回 400。根因见 turn.py::_spawn_sub_runner 的 related_call_id。

- Defect 2（中段 system）：resume 落的 ``suspend_resolved`` 内部记账 marker 被
  ``history_to_api_messages`` 渲染成 ``role="system"``，openai_compat 原样透传 →
  对话中段出现 system 消息（anthropic/gemini provider 都特判丢弃/转 user，唯独
  openai_compat 没处理）→ 严格代理回 400。该 marker 是幂等记账，LLM 不该看见。

MockClient 不校验 tool_call_id 配对，故既有 e2e 测不到——这里用渲染后断言补齐。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    system_injection,
    user_message,
)
from taifeng.loop.prompt import history_to_api_messages
from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.types import ApiMessage


# ---------------------------------------------------------------------------
# OpenAI 消息序列合法性校验（严格代理口径）
# ---------------------------------------------------------------------------
def assert_openai_valid(msgs: list[ApiMessage]) -> None:
    """断言 messages 数组对 OpenAI chat 格式合法，否则 AssertionError 带定位。

    规则：
      1. tool 消息必须紧跟在含其 tool_call_id 的 assistant(tool_calls) 之后
         （中间只能是其它 tool 消息）—— 否则 orphan tool。
      2. assistant(tool_calls) 后必须紧跟覆盖每个 id 的 tool 消息 —— 否则 tool_calls 未应答。
      3. system 角色只允许出现在首位（多数 OpenAI-compat 代理拒绝中段 system）。
    """
    errs: list[str] = []
    for i, m in enumerate(msgs):
        if m.role == "system" and i != 0:
            errs.append(f"[{i}] 中段 system 消息: {m.content[:32]!r}")
        if m.role == "tool":
            j = i - 1
            while j >= 0 and msgs[j].role == "tool":
                j -= 1
            ids = [t["id"] for t in (msgs[j].tool_calls or [])] if j >= 0 else []
            if j < 0 or msgs[j].role != "assistant" or m.tool_call_id not in ids:
                errs.append(f"[{i}] orphan tool(tool_call_id={m.tool_call_id})")
        if m.role == "assistant" and m.tool_calls:
            ids = [t["id"] for t in m.tool_calls]
            nxt = msgs[i + 1 : i + 1 + len(ids)]
            if [x.tool_call_id for x in nxt] != ids:
                errs.append(
                    f"[{i}] assistant(tool_calls={ids}) 后未紧跟匹配 tool，"
                    f"实得={[(x.role, x.tool_call_id) for x in nxt]}"
                )
    assert not errs, "OpenAI 非法消息序列:\n  " + "\n  ".join(errs)


# ---------------------------------------------------------------------------
# Defect 2 单元：history_to_api_messages 不渲染 suspend_resolved 内部 marker
# ---------------------------------------------------------------------------
def test_suspend_resolved_marker_not_rendered() -> None:
    """suspend_resolved 是幂等记账 marker，不应进 LLM 视图；业务/记忆类 system 保留。"""
    tid = "t"
    hist = [
        user_message("hi", thread_id=tid),
        system_injection("suspend_resolved:rec_1", thread_id=tid, source="suspend_resolved"),
        system_injection("业务系统提示", thread_id=tid, source="business"),
        system_injection("记忆摘要", thread_id=tid, source="memory_pre_evict"),
    ]
    msgs = history_to_api_messages(hist)
    contents = [m.content for m in msgs if m.role == "system"]
    assert "业务系统提示" in contents, "business 类 system 必须保留（LLM-facing）"
    assert "记忆摘要" in contents, "memory_pre_evict 类 system 必须保留（LLM-facing）"
    assert all(not c.startswith("suspend_resolved") for c in contents), (
        f"suspend_resolved 内部 marker 不应渲染，实得 system contents={contents}"
    )


# ---------------------------------------------------------------------------
# Defect 1 单元：手工拼 resume 后 root 历史 → 渲染必须 OpenAI 合法
# ---------------------------------------------------------------------------
def test_root_history_after_child_resume_renders_valid() -> None:
    """父 call_skill(CS1) 挂起→resume 回填后的 root 历史渲染必须配对合法、无中段 system。

    回填的 function_call_output 必须用父 function_call 的 call_id（CS1），而非子帧 sk_*。
    """
    tid = "root"
    hist = [
        user_message("患者数据", thread_id=tid),
        assistant_message("先做初诊", thread_id=tid, model="m"),
        function_call("CS1", "call_skill", '{"skill_id":"initial-scan"}', thread_id=tid),
        # resume 回填：output 必须用 CS1（与上面的 function_call 配对）
        function_call_output("CS1", "初诊结果", thread_id=tid),
        system_injection("suspend_resolved:rec_root", thread_id=tid, source="suspend_resolved"),
    ]
    assert_openai_valid(history_to_api_messages(hist))


# ---------------------------------------------------------------------------
# Defect 1 集成：真实 child-resume 流 → root 回填 call_id 必须匹配 + 渲染合法
# ---------------------------------------------------------------------------
_PARENT = """---
name: parent-flow
description: 父流程
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [intake-analyzer]
tool_names: []
max_call_depth: 3
---
# 父流程 PARENT_MARK
派发子 skill 完成分析。
"""

_CHILD = """---
name: intake-analyzer
description: 采集分析
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 采集分析 CHILD_MARK
缺数据时调 request_user_input。
"""


def _routing_client():
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient

    return RoutingMockClient(routes={
        "PARENT_MARK": [
            MockTurn(text="先做初诊", tool_calls=[
                {"id": "c_call", "name": "call_skill",
                 "arguments": '{"skill_id":"intake-analyzer","reason":"x"}'}]),
            MockTurn(text="父流程完成"),
        ],
        "CHILD_MARK": [
            MockTurn(text="向用户采集", tool_calls=[
                {"id": "call_rui1", "name": "request_user_input",
                 "arguments": '{"prompt":"补充体检"}'}]),
            MockTurn(text="子完成 CHILD_DONE"),
        ],
    })


class _Recorder:
    def __init__(self, engine) -> None:
        self._events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self._events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    async def wait_terminal(self, sub_id: str, *, timeout_s: float = 8.0) -> list:
        async def _poll() -> list:
            while True:
                got = [e for e in self._events if e.submission_id == sub_id]
                for e in got:
                    if e.msg.kind == "turn_suspended":
                        return got
                    if (e.msg.kind in ("turn_completed", "turn_failed")
                            and e.msg.data.get("is_root")):
                        return got
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout=timeout_s)


@pytest.mark.asyncio
async def test_child_resume_root_backfill_call_id_matches(tmp_path: Path, threads_dir):
    """真实 resume：root 回填的 call_skill output call_id 必须 == 父 function_call 的 id。"""
    import taifeng
    from taifeng.loop.submission import Resume
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    skills = tmp_path / "skills"
    for sub, body in (("parent-flow", _PARENT), ("intake-analyzer", _CHILD)):
        d = skills / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=_routing_client(),
        compressors=[], extra_tools=[make_request_user_input_tool()])
    engine = await pool.get_or_create(session_id="e", entry_skill_id="parent-flow")
    root_tid = engine.thread_id

    rec = _Recorder(engine)
    await asyncio.sleep(0)

    sub = await engine.submit(taifeng.UserMessage(text="go"))
    ev1 = await rec.wait_terminal(sub)
    susp = next(e for e in ev1 if e.msg.kind == "turn_suspended")
    child_tid = susp.msg.data["thread_id"]
    citems = [it async for it in await pool.store.load_thread(child_tid)]
    record = SuspensionRecord.from_item([it for it in citems if it.kind == "suspension"][0])
    rid = record.pending[0].request_id

    rsub = await engine.submit(Resume(thread_id=child_tid, resolutions={rid: {"v": "ok"}}))
    await rec.wait_terminal(rsub)

    # === root 线程：function_call(call_skill) 的 call_id 必须 == 回填 output 的 call_id ===
    ritems = [it async for it in await pool.store.load_thread(root_tid)]
    await pool.close()

    fc = [it for it in ritems if it.kind == "function_call"
          and it.payload.get("name") == "call_skill"]
    assert fc, "root 应有 call_skill 的 function_call"
    fc_id = fc[0].payload["call_id"]  # = "c_call"（LLM 给的 id）
    fco_ids = {it.payload.get("call_id") for it in ritems
               if it.kind == "function_call_output"}
    assert fc_id in fco_ids, (
        f"root 回填 output 的 call_id 必须匹配 function_call({fc_id})，"
        f"实得 {fco_ids}（说明用了子帧 sk_* → orphan tool → OpenAI 400）"
    )

    # === 渲染后整体 OpenAI 合法（配对 + 无中段 system）===
    assert_openai_valid(history_to_api_messages(ritems))
