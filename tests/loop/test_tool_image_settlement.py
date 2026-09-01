"""工具返回图片附件的结算链：ToolResult → fco payload → 下一轮请求。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ImagePart
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

SKILL = """---
name: watcher
description: 取图观察
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [observe_frame]
max_call_depth: 3
---
# 观察者
需要看画面时调用 observe_frame。
"""

IMAGE_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),
    provider="sim",
    protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}),
)

ENABLED_POLICY = ImageInputPolicy(
    enabled=True,
    max_images=2,
    max_item_bytes=4096,
    max_total_bytes=4096,
    allowed_media_types=frozenset({"image/png"}),
)


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _observe_frame_tool() -> ToolSpec:
    """业务侧取图工具的最小样例 —— 内核不提供此实现，仅消费其附件。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        attachment = ImageAttachmentV1.from_bytes(_png(), media_type="image/png")
        return ToolResult.ok("frame 1023", attachments=(attachment,))

    return ToolSpec(
        name="observe_frame",
        description="取一帧画面",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "watcher").mkdir(parents=True)
    (root / "watcher" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return root


async def _run(
    tmp_path: Path, threads_dir: Path, policy: ImageInputPolicy
) -> tuple[SimClient, list]:
    """跑一轮「模型调取图工具 → 收结果继续」，返回 client 与 thread history。"""
    client = SimClient(
        turns=[
            SimTurn(
                tool_calls=[
                    {"id": "call_1", "name": "observe_frame", "arguments": "{}"}
                ]
            ),
            SimTurn(text="看到了"),
        ],
        capabilities=IMAGE_CAPS,
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path),
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=policy,
        extra_tools=[_observe_frame_tool()],
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="watcher")
    submission_id = await engine.submit(taifeng.UserMessage(text="看一下"))
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed", event.msg.data
            break
    history = engine.history_snapshot()
    await pool.close()
    return client, history


def _fco(history: list):
    return next(it for it in history if it.kind == "function_call_output")


@pytest.mark.asyncio
async def test_tool_attachments_land_in_function_call_output(
    tmp_path: Path, threads_dir: Path
) -> None:
    """工具返回的附件必须落进配对 fco 的 payload。"""
    _, history = await _run(tmp_path, threads_dir, ENABLED_POLICY)

    fco = _fco(history)
    assert fco.payload["output"] == "frame 1023"
    assert len(fco.payload["attachments"]) == 1
    assert fco.payload["attachments"][0]["kind"] == "image"
    assert fco.payload["is_error"] is False


@pytest.mark.asyncio
async def test_settled_image_reaches_the_next_request(
    tmp_path: Path, threads_dir: Path
) -> None:
    """落史之后必须真的重放进下一轮请求 —— 否则模型仍然看不见。"""
    client, _ = await _run(tmp_path, threads_dir, ENABLED_POLICY)

    second = client.ledger.requests()[1].request
    tool_msg = next(m for m in second.messages if m.role == "tool")

    assert isinstance(tool_msg.content, list)
    assert any(isinstance(p, ImagePart) for p in tool_msg.content)


@pytest.mark.asyncio
async def test_policy_disabled_marks_call_as_error_and_keeps_pairing(
    tmp_path: Path, threads_dir: Path
) -> None:
    """策略未启用 → 该次调用判错，但 fc/fco 必须仍然配对（协议硬要求）。

    不可上抛出批：那会留下无 output 的悬空 function_call，配对断裂直接 400。
    """
    from taifeng.llm.image_input import DISABLED_IMAGE_POLICY

    _, history = await _run(tmp_path, threads_dir, DISABLED_IMAGE_POLICY)

    calls = [it for it in history if it.kind == "function_call"]
    fco = _fco(history)

    assert len(calls) == 1
    assert fco.payload["call_id"] == calls[0].payload["call_id"]
    assert fco.payload["is_error"] is True
    assert "tool_attachment_rejected" in fco.payload["output"]
    assert "attachments" not in fco.payload


@pytest.mark.asyncio
async def test_rejection_reason_is_visible_to_the_model(
    tmp_path: Path, threads_dir: Path
) -> None:
    """拒绝原因必须进入模型视野，不能只落 telemetry —— 否则模型无从自我纠正。"""
    from taifeng.llm.image_input import DISABLED_IMAGE_POLICY

    client, _ = await _run(tmp_path, threads_dir, DISABLED_IMAGE_POLICY)

    second = client.ledger.requests()[1].request
    tool_msg = next(m for m in second.messages if m.role == "tool")

    assert "tool_attachment_rejected" in str(tool_msg.content)
