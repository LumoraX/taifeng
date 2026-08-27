"""EnginePool 图片策略到 LLM request 的完整注入链。"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedModalityError
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ImagePart, TextPart

if TYPE_CHECKING:
    from pathlib import Path


def _image() -> dict[str, object]:
    """生成 1×1 PNG canonical attachment。"""
    data = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    )
    return ImageAttachmentV1(
        media_type="image/png",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"),
    ).model_dump()


@pytest.mark.asyncio
async def test_pool_injects_enabled_image_policy_into_request(
    skills_dir: Path, threads_dir: Path
) -> None:
    """业务显式启用后，Engine 发给 client 的请求必须保留 text/image 顺序。"""
    client = SimClient(
        turns=[SimTurn(text="seen")],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )
    policy = ImageInputPolicy(
        enabled=True,
        max_images=1,
        max_item_bytes=1024,
        max_total_bytes=1024,
        allowed_media_types=frozenset({"image/png"}),
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=policy,
    )
    engine = await pool.get_or_create(session_id="image", entry_skill_id="code-reviewer")

    submission_id = await engine.submit(taifeng.UserMessage(text="inspect", attachments=[_image()]))
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed"
            break

    request = client.ledger.single_request().request
    await pool.close()

    assert isinstance(request.messages[-1].content, list)
    assert isinstance(request.messages[-1].content[0], TextPart)
    assert isinstance(request.messages[-1].content[1], ImagePart)


@pytest.mark.asyncio
async def test_text_only_client_rejects_image_before_conversation_append(
    skills_dir: Path, threads_dir: Path
) -> None:
    """client capability 不匹配时不能留下会在每次恢复时报错的脏历史。"""
    client = SimClient(turns=[SimTurn(text="must not run")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=ImageInputPolicy(
            enabled=True,
            max_images=1,
            max_item_bytes=1024,
            max_total_bytes=1024,
            allowed_media_types=frozenset({"image/png"}),
        ),
    )
    engine = await pool.get_or_create(session_id="reject", entry_skill_id="code-reviewer")

    with pytest.raises(UnsupportedModalityError):
        await engine.submit(taifeng.UserMessage(text="inspect", attachments=[_image()]))

    stream = await pool.store.load_thread(engine.thread_id)
    items = [item async for item in stream]
    await pool.close()

    assert items == []
    assert client.ledger.requests() == []
