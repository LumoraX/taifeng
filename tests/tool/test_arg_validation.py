"""tool-argument-validation —— 工具参数按 input_schema 预校验 + 改参反馈。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.tool.arg_validation import (
    arguments_rejection,
    check_tool_arguments,
    schema_violations,
)
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolResult, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "days": {"type": "integer"},
        "unit": {"enum": ["c", "f"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "opts": {
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
            "required": ["verbose"],
        },
    },
    "required": ["city"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"city": "北京"}, []),
        ({}, ["$: missing required property 'city'"]),
        ({"city": 1}, ["$.city: expected string, got integer"]),
        ({"city": "x", "days": True}, ["$.days: expected integer, got boolean"]),
        ({"city": "x", "unit": "k"}, ["$.unit: 'k' is not one of ['c', 'f']"]),
        ({"city": "x", "extra": 1}, ["$: unexpected property 'extra'"]),
        ({"city": "x", "tags": ["a", 2]}, ["$.tags[1]: expected string, got integer"]),
        ({"city": "x", "opts": {}}, ["$.opts: missing required property 'verbose'"]),
    ],
)
def test_schema_violations_reports_definite_errors(
    arguments: dict[str, Any], expected: list[str],
) -> None:
    """覆盖子集内的每类违例；合法参数零违例。"""
    assert schema_violations(SCHEMA, arguments) == expected


def test_schema_violations_ignores_unknown_keywords() -> None:
    """不认识的关键字（pattern / format / oneOf）放过——不误拦合法调用。"""
    schema = {"type": "object", "properties": {
        "email": {"type": "string", "format": "email", "pattern": "^x"},
        "any": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
    }}
    assert schema_violations(schema, {"email": "not-an-email", "any": 3.5}) == []


def test_schema_violations_union_type_and_number() -> None:
    """联合类型任一满足即可；integer 值满足 number。"""
    schema = {"type": "object", "properties": {
        "v": {"type": ["string", "null"]}, "n": {"type": "number"}}}
    assert schema_violations(schema, {"v": None, "n": 3}) == []
    assert schema_violations(schema, {"v": 1}) == ["$.v: expected string|null, got integer"]


def test_empty_or_missing_schema_never_rejects() -> None:
    """空 schema → 无从判定，不拦。"""
    assert schema_violations({}, {"anything": 1}) == []


def test_feedback_includes_violations_and_schema() -> None:
    """反馈文本 = 违例清单 + 未执行声明 + 期望 schema。"""
    text = check_tool_arguments(SCHEMA, {})
    assert text is not None
    assert text.startswith("invalid_arguments: $: missing required property 'city'")
    assert "not executed" in text
    assert '"required":["city"]' in text


def test_feedback_truncates_huge_schema() -> None:
    """超大 schema 截断回显，防撑爆上下文。"""
    huge = {"type": "object", "required": ["x"],
            "properties": {f"p{i}": {"type": "string", "description": "d" * 50} for i in range(200)}}
    text = check_tool_arguments(huge, {})
    assert text is not None and text.endswith("…(truncated)")
    assert len(text) < 2600


def test_arguments_rejection_prefers_parse_error_and_skips_unregistered() -> None:
    """解析错误优先；未注册工具 / 无注册表只查解析错误。"""
    reg = ToolRegistry()
    assert arguments_rejection(reg, "x", {}, "invalid_json: boom") == "invalid_arguments: invalid_json: boom"
    assert arguments_rejection(reg, "unknown", {}, None) is None
    assert arguments_rejection(None, "unknown", {}, None) is None


async def test_invalid_arguments_rejected_then_model_rewrites(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """端到端：错参不执行 handler、反馈给模型；模型按 schema 改参后正常执行一次。"""
    executed: list[dict[str, Any]] = []

    async def _handler(args: dict, ctx: object) -> ToolResult:
        executed.append(args)
        return ToolResult.ok("晴")

    weather = ToolSpec(name="weather", description="查天气", input_schema=SCHEMA,
                       handler=_handler)
    client = SimClient(turns=[
        SimTurn(text="", tool_calls=[
            {"id": "w1", "name": "weather", "arguments": '{"city": 42}'}]),
        SimTurn(text="", tool_calls=[
            {"id": "w2", "name": "weather", "arguments": '{"city": "北京"}'}]),
        SimTurn(text="北京晴"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[weather])
    engine = await pool.get_or_create(session_id="av", entry_skill_id="code-reviewer")
    from dataclasses import replace

    entry = engine._entry_skill  # noqa: SLF001
    engine._entry_skill = replace(  # noqa: SLF001
        entry, tool_names=frozenset({*entry.tool_names, "weather"}))

    sub_id = await engine.submit(taifeng.UserMessage(text="北京天气"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed", ev.msg.data
            break

    assert executed == [{"city": "北京"}]
    outputs = {it.payload["call_id"]: it.payload for it in engine.history_snapshot()
               if it.kind == "function_call_output"}
    assert outputs["w1"]["is_error"] is True
    assert "$.city: expected string, got integer" in outputs["w1"]["output"]
    assert outputs["w2"]["output"] == "晴"
    await pool.close()
