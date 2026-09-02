"""子 skill 自主取图 —— 需求的核心场景与其隔离不变量。

验证两件事：
1. 经 ``call_skill`` 派下去的**子** skill 能在自己的 thread 里反复取图并看见图；
2. **父 thread 全程不含任何图片**（call_skill 只回字符串），子 thread 因此是
   天然的视觉沙盒。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.transcript import JsonlMessageStore
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from pathlib import Path

ENTRY = """---
name: planner
description: 规划者，自己不看图
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [watcher]
max_call_depth: 3
---
# 规划者
需要看画面时派 watcher 去看，你自己不看图。
"""

CHILD = """---
name: watcher
description: 取图观察
version: 1.0.0
type: composite
model: mock-model
tool_names: [observe_frame]
max_call_depth: 3
---
# 观察者
用 observe_frame 取帧，看完给出文字结论。
"""

IMAGE_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),
    provider="sim",
    protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}),
)
POLICY = ImageInputPolicy(
    enabled=True,
    max_images=4,
    max_item_bytes=4096,
    max_total_bytes=8192,
    allowed_media_types=frozenset({"image/png"}),
)


def _png(seed: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + seed.to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _observe_frame_tool() -> ToolSpec:
    """业务侧取图工具：每次调用返回不同的一帧。"""
    counter = {"n": 0}

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        counter["n"] += 1
        attachment = ImageAttachmentV1.from_bytes(
            _png(counter["n"]), media_type="image/png"
        )
        return ToolResult.ok(f"frame {counter['n']}", attachments=(attachment,))

    return ToolSpec(
        name="observe_frame",
        description="取一帧画面",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "planner").mkdir(parents=True)
    (root / "planner" / "SKILL.md").write_text(ENTRY, encoding="utf-8")
    (root / "watcher").mkdir(parents=True)
    (root / "watcher" / "SKILL.md").write_text(CHILD, encoding="utf-8")
    return root


async def _run(tmp_path: Path, threads_dir: Path) -> tuple[list, dict[str, list]]:
    """父派子 → 子连续两轮取图 → 子给结论 → 父收字符串收尾。

    Returns:
        (父 thread history, {子 thread_id: history})
    """
    client = SimClient(
        turns=[
            # 父：派 watcher
            SimTurn(
                tool_calls=[{
                    "id": "call_dispatch",
                    "name": "call_skill",
                    "arguments": '{"skill_id": "watcher", "args": {"input": "看两帧"}}',
                }]
            ),
            # 子第 1 轮：取第一帧
            SimTurn(
                tool_calls=[{"id": "f1", "name": "observe_frame", "arguments": "{}"}]
            ),
            # 子第 2 轮：看过第一帧后决定再取一帧（判断权在子手里）
            SimTurn(
                tool_calls=[{"id": "f2", "name": "observe_frame", "arguments": "{}"}]
            ),
            # 子第 3 轮：给文字结论
            SimTurn(text="两帧均正常"),
            # 父：收到字符串后收尾
            SimTurn(text="已确认"),
        ],
        capabilities=IMAGE_CAPS,
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path),
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=POLICY,
        extra_tools=[_observe_frame_tool()],
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="planner")
    submission_id = await engine.submit(taifeng.UserMessage(text="看一下画面"))
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed", event.msg.data
            break
    parent_thread_id = engine.thread_id
    await pool.close()

    # 一律从 store 读持久化真相（内存快照不是 R5 的事实源）
    store = JsonlMessageStore(threads_dir)
    parent_history: list = []
    children: dict[str, list] = {}
    for meta in await store.list_threads():
        items = [item async for item in await store.load_thread(meta.thread_id)]
        if meta.thread_id == parent_thread_id:
            parent_history = items
        else:
            children[meta.thread_id] = items
    return parent_history, children


def _images_in(history: list) -> list[dict]:
    """history 里所有 item 携带的图片附件（不分 kind）。"""
    found: list[dict] = []
    for item in history:
        found.extend(item.payload.get("attachments") or [])
    return found


@pytest.mark.asyncio
async def test_child_skill_sees_images_it_fetched_itself(
    tmp_path: Path, threads_dir: Path
) -> None:
    """子 skill 连续两轮自主取图，两张图都落在它自己的 thread 里。"""
    _, children = await _run(tmp_path, threads_dir)

    assert len(children) == 1, "应恰好派生一个子 thread"
    child_history = next(iter(children.values()))
    images = _images_in(child_history)

    assert len(images) == 2, "两轮取图应各留一张，验证「反复取图」而非一次性输入"
    assert {img["sha256"] for img in images}.__len__() == 2, "两帧必须是不同的图"


@pytest.mark.asyncio
async def test_images_stay_inside_the_child_thread(
    tmp_path: Path, threads_dir: Path
) -> None:
    """视觉沙盒不变量：图全在子 thread，父 thread 一张都没有。

    父子放在同一条里对比，避免「父为空所以断言恒真」的空过——子侧非零是本
    断言有判别力的前提。
    """
    parent_history, children = await _run(tmp_path, threads_dir)
    child_history = next(iter(children.values()))

    assert len(_images_in(child_history)) == 2, "前提：子侧确实拿到了图"
    assert _images_in(parent_history) == [], "父 thread 不得承载任何图片"


@pytest.mark.asyncio
async def test_child_conclusion_is_plain_text(
    tmp_path: Path, threads_dir: Path
) -> None:
    """子的产出是纯文本结论 —— 回传父层的是字符串，不是像素。"""
    _, children = await _run(tmp_path, threads_dir)
    child_history = next(iter(children.values()))

    finals = [it for it in child_history if it.kind == "assistant_message"]
    assert "两帧均正常" in finals[-1].payload["text"]
    assert not (finals[-1].payload.get("attachments") or [])
