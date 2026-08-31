"""reasoning-content-passback 回归:落史 + prompt 重建回传 + provider 组装。

thinking 模型(deepseek-v4/r 系等)要求带 tool_calls 的 assistant 消息续传时
回传 reasoning_content;本组测试覆盖三层:
1. 落史:reasoning item 与 assistant message 配对落史(顺序/零变化/无产出不落)
2. 重建:history_to_api_messages 把 reasoning 附到紧随其后首条 assistant 消息
3. 组装:openai_compat / litellm 把 ApiMessage.reasoning 翻译为 reasoning_content
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    reasoning,
    user_message,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.prompt import history_to_api_messages

if TYPE_CHECKING:
    from pathlib import Path

TID = "t-test"


# === 1. 落史 =================================================================


@pytest.mark.asyncio
async def test_reasoning_persisted_before_assistant(
    skills_dir: Path, threads_dir: Path
) -> None:
    """thinking 轮落史顺序:reasoning → assistant_message → function_call。"""
    client = SimClient(turns=[
        SimTurn(reasoning="先想清楚要读哪个 skill", text="", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id": "style-checker"}'},
        ]),
        SimTurn(reasoning="读完了,可以给结论", text="结论如下。"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="请审查"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            break

    gen = await pool.store.load_thread(engine.thread_id)
    items = [it async for it in gen]
    await pool.close()

    kinds = [it.kind for it in items]
    # 第一轮:reasoning 紧邻其配对 assistant message 之前,fc/fco 在其后
    assert kinds[:5] == [
        "user_message", "reasoning", "assistant_message",
        "function_call", "function_call_output",
    ]
    # 第二轮同样配对;reasoning 全文 = 全部 delta 拼接
    r_items = [it for it in items if it.kind == "reasoning"]
    assert [it.payload["text"] for it in r_items] == [
        "先想清楚要读哪个 skill", "读完了,可以给结论",
    ]


@pytest.mark.asyncio
async def test_no_reasoning_zero_change(
    skills_dir: Path, threads_dir: Path
) -> None:
    """非 thinking 模型(无 reasoning_delta):不落 reasoning item,history 与旧版一致。"""
    client = SimClient(turns=[SimTurn(text="直接回答。")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="你好"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    gen = await pool.store.load_thread(engine.thread_id)
    items = [it async for it in gen]
    await pool.close()
    assert all(it.kind != "reasoning" for it in items)


@pytest.mark.asyncio
async def test_reasoning_without_output_not_persisted(
    skills_dir: Path, threads_dir: Path
) -> None:
    """纯 reasoning 无产出轮(text 空且无 tool_calls):不落——没有可关联的 assistant 消息。"""
    client = SimClient(turns=[SimTurn(reasoning="只想不说", text="")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="嗯"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    gen = await pool.store.load_thread(engine.thread_id)
    items = [it async for it in gen]
    await pool.close()
    assert all(it.kind != "reasoning" for it in items)


# === 2. prompt 重建回传 ======================================================


def _tool_call_history() -> list[ResponseItem]:
    """挂起恢复续跑的典型史:reasoning → assistant(空文本) → fc → fco。"""
    return [
        user_message("帮我查一下", thread_id=TID),
        reasoning("我需要先调用工具", thread_id=TID),
        assistant_message("", thread_id=TID, model="m"),
        function_call(call_id="c1", name="ask", arguments="{}", thread_id=TID),
        function_call_output(call_id="c1", output="答案", thread_id=TID),
    ]


def test_rebuild_merges_round_and_attaches_reasoning() -> None:
    """同轮合并:assistant 文本 + fc 归并为一条消息,reasoning 附在其上。

    thinking 模型校验每条带 tool_calls 的 assistant 消息必须带
    reasoning_content,合并是唯一干净解(拆多条形态被 deepseek 真实 400)。
    """
    msgs = history_to_api_messages(_tool_call_history())
    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1].reasoning == "我需要先调用工具"
    assert [tc["id"] for tc in (msgs[1].tool_calls or [])] == ["c1"]


def test_rebuild_preserves_tool_call_extra_content() -> None:
    """function_call 的 provider 扩展字段必须回放进下一轮 tool_calls。"""
    items = [
        user_message("帮我查一下", thread_id=TID),
        assistant_message("", thread_id=TID, model="m"),
        function_call(
            call_id="c1",
            name="ask",
            arguments="{}",
            thread_id=TID,
            extra_content={"google": {"thought_signature": "sig-1"}},
        ),
        function_call_output(call_id="c1", output="答案", thread_id=TID),
    ]
    msgs = history_to_api_messages(items)
    assert msgs[1].tool_calls == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "ask", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "sig-1"}},
        }
    ]


def test_rebuild_merges_parallel_fc_interleaved() -> None:
    """同轮并行双 fc(落史配对交错 fc,fco,fc,fco):全部归并到该轮 assistant。"""
    items = [
        user_message("规划行程", thread_id=TID),
        reasoning("要并行问两个问题", thread_id=TID),
        assistant_message("", thread_id=TID, model="m"),
        function_call(call_id="c0", name="ask", arguments="{}", thread_id=TID),
        function_call_output(call_id="c0", output="杭州", thread_id=TID),
        function_call(call_id="c1", name="ask", arguments="{}", thread_id=TID),
        function_call_output(call_id="c1", output="3000元", thread_id=TID),
    ]
    msgs = history_to_api_messages(items)
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "tool"]
    assert msgs[1].reasoning == "要并行问两个问题"
    assert [tc["id"] for tc in (msgs[1].tool_calls or [])] == ["c0", "c1"]
    assert [m.tool_call_id for m in msgs[2:]] == ["c0", "c1"]


def test_rebuild_knob_off_drops_reasoning() -> None:
    """旋钮关闭:reasoning 丢弃,消息序与合并形态一致但无 reasoning 字段。"""
    msgs = history_to_api_messages(_tool_call_history(), include_reasoning=False)
    assert [m.role for m in msgs] == ["user", "assistant", "tool"]
    assert all(m.reasoning is None for m in msgs)


def test_rebuild_orphan_reasoning_skipped() -> None:
    """孤儿 reasoning(其后首条产出消息非 assistant):确定性跳过,不附到 user/tool。"""
    items = [
        user_message("a", thread_id=TID),
        reasoning("被压缩剪成孤儿的思考", thread_id=TID),
        user_message("b", thread_id=TID),
        assistant_message("回复", thread_id=TID, model="m"),
    ]
    msgs = history_to_api_messages(items)
    assert all(m.reasoning is None for m in msgs)


def test_rebuild_old_history_compat() -> None:
    """旧 JSONL(无 reasoning item):重建行为与旧版完全一致。"""
    items = [
        user_message("a", thread_id=TID),
        assistant_message("b", thread_id=TID, model="m"),
    ]
    msgs = history_to_api_messages(items)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert all(m.reasoning is None for m in msgs)


# === 3. provider 组装 ========================================================


def _req(messages: list[ApiMessage]) -> ApiRequest:
    return ApiRequest(model="m", messages=messages)


def test_openai_compat_payload_reasoning_content() -> None:
    """openai_compat:reasoning 非 None 写 reasoning_content;None 不写键。"""
    from taifeng.llm.providers.openai_compat import OpenAICompatSession
    from taifeng.loop.cancellation import CancellationToken

    sess = OpenAICompatSession(
        base_url="https://api.example.com/v1", api_key="k",
        model="m", cancel=CancellationToken(),
    )
    payload = sess._build_payload(_req([
        ApiMessage(role="assistant", content="x", reasoning="思考过程"),
        ApiMessage(role="assistant", content="y"),
    ]))
    m0, m1 = payload["messages"]
    assert m0["reasoning_content"] == "思考过程"
    assert "reasoning_content" not in m1


def test_litellm_messages_reasoning_content() -> None:
    """litellm:与 openai_compat 行为一致。"""
    from taifeng.llm.providers.litellm_provider import _to_litellm_messages

    msgs = _to_litellm_messages(_req([
        ApiMessage(role="assistant", content="x", reasoning="思考过程"),
        ApiMessage(role="assistant", content="y"),
    ]))
    assert msgs[0]["reasoning_content"] == "思考过程"
    assert "reasoning_content" not in msgs[1]


# === 4. verify 修复:rewind 冷推导与压缩边界对 reasoning 的处理 ==============


def test_derive_rewind_iteration_history_len_excludes_reasoning() -> None:
    """冷推导 iteration 节点的 history_len 必须与热路径一致(采样前长度,不含本轮 reasoning)。

    热路径在采样前记录 history_len(此时本轮 reasoning 尚未落史);冷 derive 若
    直接用 assistant_message 下标,reasoning 占位会使坐标偏大 1 → 热冷不一致,
    rewind 截断点错位。
    """
    from taifeng.loop.rewind import derive_rewind_log

    hist = [
        user_message("问题", thread_id=TID),                                    # idx 0
        reasoning("思考", thread_id=TID),                                       # idx 1
        assistant_message("", thread_id=TID, model="m"),                        # idx 2
        function_call(call_id="c1", name="ask", arguments="{}", thread_id=TID),  # idx 3
        function_call_output(call_id="c1", output="答", thread_id=TID),          # idx 4
    ]
    cps = derive_rewind_log(hist)
    iters = [c for c in cps if c.kind == "iteration"]
    # 采样前 buffer 只有 user_message 一项 → history_len 必须是 1(而非 am 下标 2)
    assert iters and iters[0].history_len == 1, \
        f"热冷坐标不一致: 期待 1(采样前长度), 实得 {iters[0].history_len if iters else None}"


def test_walk_back_boundary_keeps_reasoning_with_assistant() -> None:
    """压缩切分点指向 assistant_message 且其前一项是配对 reasoning 时必须一并保留。

    否则 tail 中带 tool_calls 的 assistant 轮丢失 reasoning_content,thinking
    模型对压缩后历史的续传可能再次被 provider 拒。
    """
    from taifeng.context.strategies.handoff import _walk_back_to_safe_boundary

    hist = [
        user_message("问题", thread_id=TID),                                    # idx 0
        reasoning("思考", thread_id=TID),                                       # idx 1
        assistant_message("", thread_id=TID, model="m"),                        # idx 2
        function_call(call_id="c1", name="ask", arguments="{}", thread_id=TID),  # idx 3
        function_call_output(call_id="c1", output="答", thread_id=TID),          # idx 4
    ]
    # cut 指向 am(idx 2):前一项是它的 reasoning → 应回退到 1 把 reasoning 留进 tail
    assert _walk_back_to_safe_boundary(hist, 2) == 1
    # cut 指向 reasoning 自身(idx 1):reasoning 已在 tail,无需再退
    assert _walk_back_to_safe_boundary(hist, 1) == 1
