"""附件存储端到端：先存后记、解析阶段、降级、恢复、工具结果与读回（ADR 0148）。"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import taifeng
from taifeng.context.budget import estimate_history_tokens
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import MultimodalEvictionStrategy
from taifeng.conversation.attachment_store import (
    AttachmentNotFoundError,
    LocalAttachmentStore,
    collect_attachment_refs,
)
from taifeng.conversation.models import user_message
from taifeng.llm.attachment_refs import ImageAttachmentRefV1
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import AttachmentUnavailableError, InvalidImageError
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ImagePart, TextPart
from taifeng.loop.attachment_resolve import AttachmentResolver
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.pool_attachments import build_attachment_resolver
from taifeng.tool.builtins.file_io import ATTACHMENT_REFS_EXTRAS_KEY, make_file_read_tool
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import run_until_root_done

if TYPE_CHECKING:
    from pathlib import Path

CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image"}),
    provider="sim",
    protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}),
)
POLICY = ImageInputPolicy(
    enabled=True, max_images=2, max_item_bytes=4096, max_total_bytes=8192,
    allowed_media_types=frozenset({"image/png"}),
)

WATCHER = """---
name: watcher
description: 取图观察
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [observe_frame, file_read]
max_call_depth: 3
---
# 观察者
"""


def _png(width: int = 3, height: int = 2) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big") + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


PNG = _png()
DIGEST = hashlib.sha256(PNG).hexdigest()
REF = f"sha256-{DIGEST}"


def _inline(data: bytes = PNG) -> dict[str, Any]:
    return ImageAttachmentV1.from_bytes(data, media_type="image/png").model_dump()


class CountingStore(LocalAttachmentStore):
    """本机存储 + 调用计数 + 可注入的写入失败。"""

    def __init__(self, root: Path, *, fail_put: bool = False) -> None:
        super().__init__(root)
        self.puts = 0
        self.gets = 0
        self._fail_put = fail_put

    async def put(self, data: bytes, *, media_type: str, sha256: str) -> str:
        self.puts += 1
        if self._fail_put:
            raise OSError("disk full")
        return await super().put(data, media_type=media_type, sha256=sha256)

    async def get(self, ref: str) -> bytes:
        self.gets += 1
        return await super().get(ref)


def _client(*turns: SimTurn) -> SimClient:
    return SimClient(turns=list(turns) or [SimTurn(text="seen")], capabilities=CAPS)


async def _pool(
    skills_dir: Path, threads_dir: Path, client: SimClient, store: Any = None, **kwargs: Any,
) -> taifeng.EnginePool:
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=kwargs.pop("compressors", []), image_input_policy=POLICY,
        attachment_store=store, **kwargs,
    )


def _transcripts(threads_dir: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in threads_dir.rglob("*.jsonl"))


def _images(request: Any) -> list[ImagePart]:
    return [
        part for message in request.messages if isinstance(message.content, list)
        for part in message.content if isinstance(part, ImagePart)
    ]


# ---- 写入：先存后记 ----


async def test_user_image_lands_as_reference(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "blobs")
    client = _client()
    pool = await _pool(skills_dir, threads_dir, client, store)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    history = engine.history_snapshot()
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    (attachment,) = next(i for i in history if i.kind == "user_message").payload["attachments"]
    assert attachment["encoding"] == "store" and attachment["ref"] == REF
    assert (attachment["width"], attachment["height"]) == (3, 2)  # 来自字节，不是声明
    assert await store.get(REF) == PNG
    assert _inline()["content"] not in _transcripts(threads_dir)
    stored = [e.msg for e in events if e.msg.kind == "attachment_stored"]
    assert stored and stored[0].data["origin"] == "user_message"
    assert "content" not in stored[0].data and _inline()["content"] not in str(stored[0].data)
    assert len(_images(client.ledger.single_request().request)) == 1


async def test_request_is_identical_to_inline(skills_dir: Path, tmp_path: Path) -> None:
    """同一张图：引用形态与内联形态发给 provider 的消息逐字段相同（R2）。"""
    results = []
    for store in (None, LocalAttachmentStore(tmp_path / "blobs")):
        client = _client()
        threads = tmp_path / ("t-store" if store else "t-inline")
        threads.mkdir()
        pool = await _pool(skills_dir, threads, client, store)
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
        results.append([m.model_dump() for m in client.ledger.single_request().request.messages])
        await pool.close()
    assert results[0] == results[1]


async def test_store_write_failure_rejects_submission(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "blobs", fail_put=True)
    pool = await _pool(skills_dir, threads_dir, _client(), store)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    with pytest.raises(AttachmentUnavailableError):
        await engine.submit(taifeng.UserMessage(text="看图", attachments=[_inline()]))
    history = engine.history_snapshot()
    await pool.close()
    assert not any(item.kind == "user_message" for item in history)
    assert "看图" not in _transcripts(threads_dir)


async def test_rejected_attachment_is_never_stored(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "blobs")
    pool = await _pool(skills_dir, threads_dir, _client(), store)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    broken = {**_inline(), "sha256": "0" * 64}
    with pytest.raises(InvalidImageError):
        await engine.submit(taifeng.UserMessage(text="看图", attachments=[broken]))
    await pool.close()
    assert store.puts == 0


# ---- 宿主直接传引用 ----


async def test_host_uploaded_reference_is_verified(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    """先上传后引用：声明的宽高被忽略，以字节为准；模型收到同一张图。"""
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = _client()
    pool = await _pool(skills_dir, threads_dir, client, store)
    assert pool.attachments is store
    ref = await store.put(PNG, media_type="image/png", sha256=DIGEST)
    forged = ImageAttachmentRefV1(
        ref=ref, media_type="image/png", size=len(PNG), sha256=DIGEST, width=999, height=999,
    ).model_dump()
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[forged]))
    history = engine.history_snapshot()
    await pool.close()

    assert events[-1].msg.kind == "turn_completed"
    (attachment,) = next(i for i in history if i.kind == "user_message").payload["attachments"]
    assert (attachment["width"], attachment["height"]) == (3, 2)
    (image,) = _images(client.ledger.single_request().request)
    assert base64.b64decode(image.base64_data) == PNG


@pytest.mark.parametrize("problem", ["missing", "wrong_digest"])
async def test_bad_host_reference_is_rejected(
    skills_dir: Path, threads_dir: Path, tmp_path: Path, problem: str,
) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    pool = await _pool(skills_dir, threads_dir, _client(), store)
    if problem == "wrong_digest":
        await store.put(PNG, media_type="image/png", sha256=DIGEST)
    digest = DIGEST if problem == "missing" else hashlib.sha256(b"other").hexdigest()
    ref = ImageAttachmentRefV1(
        ref=REF if problem == "wrong_digest" else "sha256-" + "a" * 64,
        media_type="image/png", size=len(PNG), sha256=digest, width=3, height=2,
    ).model_dump()
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    with pytest.raises(AttachmentUnavailableError):
        await engine.submit(taifeng.UserMessage(text="看图", attachments=[ref]))
    await pool.close()


# ---- 读取：降级 ----


@pytest.mark.parametrize("damage", ["deleted", "tampered"])
async def test_unavailable_history_attachment_degrades(
    skills_dir: Path, threads_dir: Path, tmp_path: Path, damage: str,
) -> None:
    """历史附件取不回：占位文本 + 事件，turn 继续；之后的轮次不受影响。"""
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = _client(SimTurn(text="1"), SimTurn(text="2"), SimTurn(text="3"))
    pool = await _pool(skills_dir, threads_dir, client, store, attachment_cache_bytes=0)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    target = store.root / DIGEST[:2] / DIGEST
    if damage == "deleted":
        target.unlink()
    else:
        target.write_bytes(_png(9, 9))
    second = await run_until_root_done(engine, taifeng.UserMessage(text="再说说"))
    third = await run_until_root_done(engine, taifeng.UserMessage(text="继续"))
    await pool.close()

    assert second[-1].msg.kind == "turn_completed"
    assert third[-1].msg.kind == "turn_completed"
    (event,) = [e.msg for e in second if e.msg.kind == "attachment_unavailable"]
    assert event.data["reason"] == ("not_found" if damage == "deleted" else "integrity_mismatch")
    request = client.ledger.requests()[1].request
    assert _images(request) == []
    texts = " ".join(
        part.text if isinstance(part, TextPart) else str(part)
        for m in request.messages
        for part in (m.content if isinstance(m.content, list) else [m.content])
    )
    assert "[attachment unavailable:" in texts and REF in texts


# ---- 恢复 ----


async def test_resume_without_store_fails_before_any_request(
    skills_dir: Path, threads_dir: Path, tmp_path: Path,
) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    pool = await _pool(skills_dir, threads_dir, _client(), store)
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    thread_id = engine.thread_id
    await pool.close()

    bare = _client()
    pool = await _pool(skills_dir, threads_dir, bare, None)
    engine = await pool.get_or_create(
        session_id="s2", entry_skill_id="code-reviewer", resume_thread_id=thread_id,
    )
    events = await run_until_root_done(engine, taifeng.UserMessage(text="继续"))
    await pool.close()
    assert events[-1].msg.kind == "turn_failed"
    assert events[-1].msg.data["kind"] == "AttachmentStoreRequiredError"
    assert events[-1].msg.data["failure_class"] == "invalid_request"
    assert bare.ledger.requests() == []

    again = _client()
    pool = await _pool(skills_dir, threads_dir, again, LocalAttachmentStore(tmp_path / "blobs"))
    engine = await pool.get_or_create(
        session_id="s3", entry_skill_id="code-reviewer", resume_thread_id=thread_id,
    )
    events = await run_until_root_done(engine, taifeng.UserMessage(text="继续"))
    await pool.close()
    assert events[-1].msg.kind == "turn_completed"
    assert len(_images(again.ledger.single_request().request)) == 1


# ---- 估算与解析器 ----


async def test_estimation_reads_metadata_only(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "blobs")
    ref = await store.put(PNG, media_type="image/png", sha256=DIGEST)
    as_ref = ImageAttachmentRefV1(
        ref=ref, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    ).model_dump()
    by_ref = estimate_history_tokens(
        [user_message("x", thread_id="t", attachments=[as_ref])], image_input_policy=POLICY,
    )
    by_inline = estimate_history_tokens(
        [user_message("x", thread_id="t", attachments=[_inline()])], image_input_policy=POLICY,
    )
    assert by_ref == by_inline
    assert store.gets == 0


async def test_resolver_cache_and_singleflight(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "blobs")
    await store.put(PNG, media_type="image/png", sha256=DIGEST)
    resolver = AttachmentResolver(store)

    async with anyio.create_task_group() as group:
        for _ in range(6):
            group.start_soon(lambda: resolver.fetch(REF, sha256=DIGEST, size=len(PNG)))
    await resolver.fetch(REF, sha256=DIGEST, size=len(PNG))
    assert store.gets == 1

    uncached = AttachmentResolver(store, cache_bytes=0)
    await uncached.fetch(REF, sha256=DIGEST, size=len(PNG))
    await uncached.fetch(REF, sha256=DIGEST, size=len(PNG))
    assert store.gets == 3


def test_resolver_rejects_negative_cache(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        AttachmentResolver(LocalAttachmentStore(tmp_path), cache_bytes=-1)


def test_store_and_audit_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="audit"):
        build_attachment_resolver(LocalAttachmentStore(tmp_path), audit=object(), cache_bytes=0)  # type: ignore[arg-type]
    assert build_attachment_resolver(None, audit=None, cache_bytes=0) is None


# ---- 工具结果 ----


def _watcher_skills(tmp_path: Path) -> Path:
    root = tmp_path / "wskills"
    (root / "watcher").mkdir(parents=True)
    (root / "watcher" / "SKILL.md").write_text(WATCHER, encoding="utf-8")
    return root


def _frame_tool(attachment: Any) -> ToolSpec:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("frame", attachments=(attachment,))

    return ToolSpec(
        name="observe_frame", description="取一帧",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


async def _tool_run(tmp_path: Path, store: Any, attachment: Any) -> tuple[SimClient, list[Any]]:
    client = _client(
        SimTurn(tool_calls=[{"id": "c1", "name": "observe_frame", "arguments": "{}"}]),
        SimTurn(text="看到了"),
    )
    threads = tmp_path / "wthreads"
    threads.mkdir(exist_ok=True)
    pool = await _pool(
        _watcher_skills(tmp_path), threads, client, store, extra_tools=[_frame_tool(attachment)],
    )
    engine = await pool.get_or_create(session_id="w", entry_skill_id="watcher")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="看一下"))
    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    history = engine.history_snapshot()
    await pool.close()
    return client, history


async def test_tool_image_lands_as_reference(tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    client, history = await _tool_run(
        tmp_path, store, ImageAttachmentV1.from_bytes(PNG, media_type="image/png"),
    )
    fco = next(i for i in history if i.kind == "function_call_output")
    (attachment,) = fco.payload["attachments"]
    assert attachment["encoding"] == "store" and attachment["ref"] == REF
    (image,) = _images(client.ledger.requests()[1].request)
    assert base64.b64decode(image.base64_data) == PNG


async def test_tool_reference_without_store_is_rejected(tmp_path: Path) -> None:
    ref = ImageAttachmentRefV1(
        ref=REF, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    )
    _, history = await _tool_run(tmp_path, None, ref)
    fco = next(i for i in history if i.kind == "function_call_output")
    assert fco.payload["is_error"] is True
    assert fco.payload["output"].startswith("tool_attachment_rejected")
    calls = [i for i in history if i.kind == "function_call"]
    assert len(calls) == 1  # 配对完整


# ---- 驱逐与读回 ----


async def test_evicting_reference_needs_no_workspace(tmp_path: Path) -> None:
    from taifeng.context.budget import ContextBudget
    from taifeng.context.compressor import CompressionContext
    from taifeng.conversation.models import assistant_message

    as_ref = ImageAttachmentRefV1(
        ref=REF, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    ).model_dump()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    history = [user_message("看图", thread_id="t", attachments=[as_ref]),
               assistant_message("好", thread_id="t", model="m")]
    for strategy in (
        MultimodalEvictionStrategy(keep_recent=0, protect_tail_messages=0),
        MultimodalEvictionStrategy(keep_recent=0, protect_tail_messages=0, file_root=workspace),
    ):
        result = await strategy.compress(
            CompressionContext(
                history=history, token_estimate=9000, budget=ContextBudget(context_window=10_000),
                cache_anchor_index=-1, phase="pre_turn",
                available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
            ),
            InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
        )
        text = result.new_history[0].payload["text"]
        assert f"path=attachment:{REF}" in text and "file_read" in text
        assert collect_attachment_refs(result.new_history) == {REF}
    assert list(workspace.rglob("*")) == []


def _ctx(refs: set[str]) -> ToolContext:
    return ToolContext(
        call_id="r1", cancel=CancellationToken(), thread_id="t",
        extras={ATTACHMENT_REFS_EXTRAS_KEY: lambda: refs},
    )


async def test_file_read_reads_reference_back(tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    await store.put(PNG, media_type="image/png", sha256=DIGEST)
    pdf = b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n%%EOF"
    pdf_ref = await store.put(pdf, media_type="application/pdf", sha256=hashlib.sha256(pdf).hexdigest())
    tool = make_file_read_tool(root_dir=tmp_path, attach_images=True, attachment_store=store)

    ok = await tool.handler({"path": f"attachment:{REF}"}, _ctx({REF}))
    assert not ok.is_error and ok.attachments[0].sha256 == DIGEST
    assert str(tmp_path) not in ok.output

    outsider = await tool.handler({"path": f"attachment:{REF}"}, _ctx(set()))
    assert outsider.data["reason"] == "attachment_not_in_thread" and outsider.attachments == ()

    not_image = await tool.handler({"path": f"attachment:{pdf_ref}"}, _ctx({pdf_ref}))
    assert not_image.data["reason"] == "attachment_not_image"

    gone = "sha256-" + "b" * 64
    missing = await tool.handler({"path": f"attachment:{gone}"}, _ctx({gone}))
    assert missing.data["reason"] == "not_found"

    bare = make_file_read_tool(root_dir=tmp_path, attach_images=True)
    unconfigured = await bare.handler({"path": f"attachment:{REF}"}, _ctx({REF}))
    assert unconfigured.data["reason"] == "attachment_store_not_configured"


async def test_evicted_reference_is_read_back_end_to_end(tmp_path: Path) -> None:
    """用户图片以引用落盘 → 被驱逐 → 模型按描述里的路径 file_read → 下一次请求里图片回来了。"""
    from taifeng.context.budget import ContextBudget

    store = LocalAttachmentStore(tmp_path / "blobs")
    client = _client(
        SimTurn(text="收到"),
        SimTurn(tool_calls=[{
            "id": "r1", "name": "file_read", "arguments": f'{{"path": "attachment:{REF}"}}',
        }]),
        SimTurn(text="是 3x2 的图"),
    )
    threads = tmp_path / "ethreads"
    threads.mkdir()
    pool = await _pool(
        _watcher_skills(tmp_path), threads, client, store,
        extra_tools=[make_file_read_tool(root_dir=tmp_path, attach_images=True, attachment_store=store)],
        compressors=[MultimodalEvictionStrategy(
            trigger_ratio=1e-6, keep_recent=0, protect_tail_messages=2, allow_head_evict=True,
        )],
        budget=ContextBudget(context_window=200_000, soft_limit_ratio=0.001),
    )
    engine = await pool.get_or_create(session_id="e", entry_skill_id="watcher")
    await run_until_root_done(engine, taifeng.UserMessage(text="先收着", attachments=[_inline()]))
    events = await run_until_root_done(engine, taifeng.UserMessage(text="再看一眼那张图"))
    history = engine.history_snapshot()
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    first_user = next(i for i in history if i.kind == "user_message")
    assert f"path=attachment:{REF}" in first_user.payload["text"]
    assert first_user.payload["attachments"] == []
    after_read = client.ledger.requests()[-1].request
    (image,) = _images(after_read)
    assert base64.b64decode(image.base64_data) == PNG
    assert store.root.exists() and len(list(store.root.rglob(DIGEST))) == 1  # 没有第二份对象


async def test_cleanup_thread_keeps_store_objects(tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    await store.put(PNG, media_type="image/png", sha256=DIGEST)
    await MultimodalEvictionStrategy(file_root=tmp_path / "ws").cleanup_thread("t")
    assert await store.get(REF) == PNG
    with pytest.raises(AttachmentNotFoundError):
        await store.get("sha256-" + "c" * 64)
