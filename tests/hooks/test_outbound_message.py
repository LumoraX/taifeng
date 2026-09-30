"""出站消息归一化（ADR 0086）：root turn 的最终回答在交给业务之前经 hook 归一。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.hooks import HookDecision, HookRegistry, HookRunner
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.outbound import (
    make_outbound_normalizer_hook,
    normalize_outbound_text,
)
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.hooks.types import OutboundMessageHook


# ------------------------------------------------------------------
# 默认归一化
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected", "notes"),
    [
        ("答案", "答案", ()),
        ("", "", ()),
        ("  答案\n\n", "答案", ("trimmed",)),
        ("  答案  \n\n", "答案", ("trailing_whitespace", "trimmed")),
        ("一\r\n二\r三", "一\n二\n三", ("newlines",)),
        ("一\n\n\n\n二", "一\n\n二", ("blank_lines",)),
        ("行尾空格   \n下一行\t\n末", "行尾空格\n下一行\n末", ("trailing_whitespace",)),
        ("<think>先想想</think>答案", "答案", ("reasoning_block",)),
        ("前<thinking>\n多行\n推理\n</thinking>后", "前后", ("reasoning_block",)),
        ("<THINK>大写</THINK>答案", "答案", ("reasoning_block",)),
        ("只有推理<think>没闭合", "只有推理<think>没闭合", ()),
        (
            "<think>a</think>\r\n一  \n\n\n\n二  ",
            "一\n\n二",
            ("reasoning_block", "newlines", "trailing_whitespace", "blank_lines", "trimmed"),
        ),
    ],
)
def test_default_normalization(raw: str, expected: str, notes: tuple[str, ...]) -> None:
    assert normalize_outbound_text(raw) == (expected, notes)


def test_normalization_is_idempotent() -> None:
    once, _ = normalize_outbound_text("<think>x</think>  一\r\n\n\n\n二  ")
    assert normalize_outbound_text(once) == (once, ())


def test_code_blocks_keep_their_inner_whitespace() -> None:
    """代码块里的缩进与空行是内容，不是排版噪声。"""
    text = "示例：\n```python\ndef f():\n\n\n\n    return 1   \n```\n完"
    assert normalize_outbound_text(text) == (text, ())


def test_reasoning_tags_inside_code_blocks_are_content() -> None:
    text = "标签写法：\n```\n<think>示例</think>\n```"
    assert normalize_outbound_text(text) == (text, ())


# ------------------------------------------------------------------
# 引擎级
# ------------------------------------------------------------------


async def _run(
    skills_dir: Path, threads_dir: Path, turns: list[SimTurn], registry: HookRegistry | None,
) -> tuple[list[Any], taifeng.AgentEngine, taifeng.EnginePool]:
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=SimClient(turns=turns),
        compressors=[], hooks=HookRunner(registry) if registry is not None else None,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="问"))

    async def collect() -> list[Any]:
        events: list[Any] = []
        async for ev in engine.subscribe(sub_id):
            events.append(ev.msg)
            if ev.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
                return events
        raise AssertionError("stream ended early")

    events = await asyncio.wait_for(collect(), timeout=GUARD_TIMEOUT_SECONDS)
    return events, engine, pool


def _outbound(events: list[Any]) -> list[dict[str, Any]]:
    return [dict(e.data) for e in events if e.kind == "outbound_message"]


async def test_no_outbound_event_without_a_handler(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """未注册出站 hook：事件流与引入前一致。"""
    events, _, pool = await _run(skills_dir, threads_dir, [SimTurn(text="答案")], None)
    assert _outbound(events) == []
    events2, _, pool2 = await _run(
        skills_dir, threads_dir / "b", [SimTurn(text="答案")], HookRegistry()
    )
    assert _outbound(events2) == []
    await pool.close()
    await pool2.close()


async def test_default_normalizer_cleans_the_final_answer(
    skills_dir: Path, threads_dir: Path,
) -> None:
    registry = HookRegistry()
    registry.register("outbound_message", make_outbound_normalizer_hook())
    raw = "<think>先想想</think>结论如下  \n\n\n\n完"

    events, engine, pool = await _run(skills_dir, threads_dir, [SimTurn(text=raw)], registry)

    assert _outbound(events) == [{
        "text": "结论如下\n\n完",
        "rewritten": True,
        "raw_chars": len(raw),
        "end_reason": "completed",
        "success": True,
        "thread_id": engine.thread_id,
    }]
    kinds = [e.kind for e in events]
    assert kinds.index("outbound_message") < kinds.index("turn_completed")
    # history 里的模型原话不被改写
    assistant = [i for i in engine.history_snapshot() if i.kind == "assistant_message"]
    assert assistant[-1].payload["text"] == raw
    await pool.close()


async def test_clean_answer_is_reported_as_not_rewritten(
    skills_dir: Path, threads_dir: Path,
) -> None:
    registry = HookRegistry()
    registry.register("outbound_message", make_outbound_normalizer_hook())

    events, _, pool = await _run(skills_dir, threads_dir, [SimTurn(text="答案")], registry)

    outbound = _outbound(events)
    assert [(o["text"], o["rewritten"]) for o in outbound] == [("答案", False)]
    await pool.close()


async def test_handlers_chain_and_see_turn_facts(
    skills_dir: Path, threads_dir: Path,
) -> None:
    seen: list[OutboundMessageHook] = []

    async def redact(hook: OutboundMessageHook, ctx: Any) -> HookDecision:
        seen.append(hook)
        return HookDecision.ok(text_override=hook.text.replace("13800000000", "[手机号]"))

    async def sign(hook: OutboundMessageHook, ctx: Any) -> HookDecision:
        seen.append(hook)
        return HookDecision.ok(text_override=hook.text + "\n—— 助手")

    registry = HookRegistry()
    registry.register("outbound_message", redact)
    registry.register("outbound_message", sign)

    events, _, pool = await _run(
        skills_dir, threads_dir, [SimTurn(text="请拨打 13800000000")], registry
    )

    assert _outbound(events)[0]["text"] == "请拨打 [手机号]\n—— 助手"
    assert [h.text for h in seen] == ["请拨打 13800000000", "请拨打 [手机号]"]
    assert seen[0].end_reason == "completed" and seen[0].success is True
    await pool.close()


async def test_broken_handler_leaves_the_text_unchanged(
    skills_dir: Path, threads_dir: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(hook: OutboundMessageHook, ctx: Any) -> HookDecision:
        raise RuntimeError("normalizer down")

    async def wrong_type(hook: OutboundMessageHook, ctx: Any) -> HookDecision:
        return HookDecision.ok(text_override=123)

    async def denies(hook: OutboundMessageHook, ctx: Any) -> HookDecision:
        return HookDecision.deny("no")

    registry = HookRegistry()
    for handler in (broken, wrong_type, denies):
        registry.register("outbound_message", handler)

    events, _, pool = await _run(skills_dir, threads_dir, [SimTurn(text="答案")], registry)

    assert [(o["text"], o["rewritten"]) for o in _outbound(events)] == [("答案", False)]
    assert events[-1].kind == "turn_completed"
    assert any("outbound_message" in r.getMessage() for r in caplog.records)
    await pool.close()


async def test_child_turns_do_not_emit_outbound_messages(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """子 skill 的结果是给父模型看的，不是出站消息。"""
    registry = HookRegistry()
    registry.register("outbound_message", make_outbound_normalizer_hook())
    turns = [
        SimTurn(text="派发", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "查风格"}'}]),
        SimTurn(text="子结论  "),
        SimTurn(text="最终结论  "),
    ]
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=SimClient(turns=turns),
        compressors=[], hooks=HookRunner(registry),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="审查"))

    async def collect() -> list[Any]:
        events: list[Any] = []
        async for ev in engine.subscribe_all():
            if ev.submission_id != sub_id:
                continue
            events.append(ev.msg)
            if ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root"):
                return events
        raise AssertionError("stream ended early")

    events = await asyncio.wait_for(collect(), timeout=GUARD_TIMEOUT_SECONDS)

    # 只有 root turn 的最终回答出站（各圈文本的拼接）；子 skill 的结论不在其中
    assert [o["text"] for o in _outbound(events)] == ["派发最终结论"]
    await pool.close()
