"""EnginePool 图片策略到 LLM request 的完整注入链。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.models import user_message
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedModalityError
from taifeng.llm.image_input import (
    ConservativeImageCostEstimator,
    ImageAttachmentV1,
    ImageInputPolicy,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.openai._shared import MAX_REQUEST_BYTES_METADATA_KEY
from taifeng.llm.types import ImagePart, TextPart
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import Submission, UserMessage
from taifeng.loop.turn import TurnRunner

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
async def test_request_capture_redacts_image_body(
    skills_dir: Path, threads_dir: Path
) -> None:
    """本地 request capture 只能记录图片描述，不能复制 base64 正文。"""
    image = _image()
    image_body = str(image["content"])
    client = SimClient(
        turns=[SimTurn(text="seen")],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        enable_request_capture=True,
        image_input_policy=ImageInputPolicy(
            enabled=True,
            max_images=1,
            max_item_bytes=1024,
            max_total_bytes=1024,
            allowed_media_types=frozenset({"image/png"}),
        ),
    )
    engine = await pool.get_or_create(session_id="capture", entry_skill_id="code-reviewer")

    submission_id = await engine.submit(
        taifeng.UserMessage(text="inspect", attachments=[image])
    )
    capture: dict[str, object] | None = None
    async for event in engine.subscribe(submission_id):
        if event.msg.kind == "llm_request_recorded":
            capture = event.msg.data
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed"
            break
    await pool.close()

    assert capture is not None
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    assert image_body not in encoded
    assert "base64_data" not in encoded
    assert '"content_redacted": true' in encoded
    assert '"media_type": "image/png"' in encoded


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


@pytest.mark.asyncio
async def test_pool_passes_request_byte_budget_to_provider_preflight(
    skills_dir: Path, threads_dir: Path
) -> None:
    """TurnRunner 必须让 provider 看见最终 wire JSON 的业务字节上限。"""
    client = SimClient(turns=[SimTurn(text="seen")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        budget=taifeng.ContextBudget(max_request_bytes=1_000_000),
    )
    engine = await pool.get_or_create(session_id="wire-limit", entry_skill_id="code-reviewer")

    submission_id = await engine.submit(taifeng.UserMessage(text="inspect"))
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            assert event.msg.kind == "turn_completed"
            break

    request = client.ledger.single_request().request
    await pool.close()

    assert request.metadata[MAX_REQUEST_BYTES_METADATA_KEY] == 1_000_000


@pytest.mark.asyncio
async def test_detached_child_runner_inherits_image_policy_and_estimator(
    skills_dir: Path, threads_dir: Path
) -> None:
    """detached spawn 子 runner 必须继承 pool 的多模态业务配置。"""
    policy = ImageInputPolicy(enabled=True)
    estimator = ConservativeImageCostEstimator(token_ceiling=1234)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=SimClient(turns=[]),
        compressors=[],
        image_input_policy=policy,
        input_cost_estimator=estimator,
    )
    engine = await pool.get_or_create(session_id="child-policy", entry_skill_id="code-reviewer")
    target = engine._snapshot.get("style-checker")  # noqa: SLF001
    assert target is not None
    child_thread_id = await pool.store.create_thread(
        cwd=None, entry_skill_id=target.id, source="test"
    )
    seed = user_message("inspect", thread_id=child_thread_id)

    runner = engine._build_child_runner(  # noqa: SLF001
        target, child_thread_id, seed, CancellationToken()
    )
    await pool.close()

    assert runner.image_input_policy is policy
    assert runner.input_cost_estimator is estimator


@pytest.mark.asyncio
async def test_resumed_child_runner_inherits_image_policy_and_estimator(
    skills_dir: Path,
    threads_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非根 thread Resume 重建的 runner 不得退回默认 text-only 策略。"""
    policy = ImageInputPolicy(enabled=True)
    estimator = ConservativeImageCostEstimator(token_ceiling=1234)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=SimClient(turns=[]),
        compressors=[],
        image_input_policy=policy,
        input_cost_estimator=estimator,
    )
    engine = await pool.get_or_create(session_id="resume-policy", entry_skill_id="code-reviewer")
    child_thread_id = await pool.store.create_thread(
        cwd=None, entry_skill_id="style-checker", source="test"
    )
    await pool.store.append(user_message("inspect", thread_id=child_thread_id))
    captured: list[TurnRunner] = []

    async def fake_run(runner: TurnRunner) -> object:
        captured.append(runner)
        return object()

    monkeypatch.setattr(TurnRunner, "run", fake_run)
    await engine._run_thread_turn(  # noqa: SLF001
        Submission(op=UserMessage(text="resume")),
        child_thread_id,
        "style-checker",
        CancellationToken(),
    )
    await pool.close()

    assert captured[0].image_input_policy is policy
    assert captured[0].input_cost_estimator is estimator


@pytest.mark.asyncio
async def test_engine_estimate_tokens_uses_injected_image_estimator(
    skills_dir: Path,
    threads_dir: Path,
) -> None:
    """公共 estimate_tokens 与 turn preflight 必须共享同一图片预算配置。"""

    class RecordingEstimator:
        """记录 engine 传入的模型与图片尺寸。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []

        def estimate_image_tokens(self, **kwargs: object) -> int:
            self.calls.append(
                (str(kwargs["model"]), int(kwargs["width"]), int(kwargs["height"]))
            )
            return 4321

    estimator = RecordingEstimator()
    policy = ImageInputPolicy(
        enabled=True,
        max_images=1,
        max_item_bytes=1024,
        max_total_bytes=1024,
        allowed_media_types=frozenset({"image/png"}),
    )
    client = SimClient(
        turns=[],
        capabilities=ModelCapabilities(
            input_modalities=frozenset({"text", "image"}),
            provider="sim",
            protocol="sim",
        ),
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        image_input_policy=policy,
        input_cost_estimator=estimator,
    )
    engine = await pool.get_or_create(
        session_id="estimate-image",
        entry_skill_id="code-reviewer",
    )
    engine._history.append(  # noqa: SLF001
        user_message("inspect", thread_id=engine.thread_id, attachments=[_image()])
    )

    estimated = engine.estimate_tokens()
    await pool.close()

    assert estimated >= 4321
    assert estimator.calls == [("mock-model", 1, 1)]
