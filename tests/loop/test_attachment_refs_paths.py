"""附件存储在其余执行路径上的端到端覆盖（ADR 0148）。

``test_attachment_refs.py`` 覆盖了根 turn 的主路径；本文件补齐其余会构建请求或改写 history 的路径：
子 skill（call_skill）、分离式 spawn 的并发子 thread、挂起 → Resume、Rewind 重推、ContextEngine 视图、
handoff 摘要、文件（PDF）附件、内联与引用混合、旧 thread 升级、多附件的全有或全无，以及解析器的
LRU / 后端异常 / 取消边界。
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import taifeng
from taifeng.context.budget import ContextBudget, estimate_history_tokens
from taifeng.context.compressor import CompressionContext
from taifeng.context.engine import TailWindowContextEngine
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.conversation.attachment_store import (
    AttachmentStoreError,
    LocalAttachmentStore,
    collect_attachment_refs,
)
from taifeng.conversation.models import assistant_message, user_message
from taifeng.llm.attachment_refs import FileAttachmentRefV1, ImageAttachmentRefV1
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import AttachmentUnavailableError
from taifeng.llm.file_input import FileAttachmentV1, FileInputPolicy
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.llm.types import FilePart, ImagePart
from taifeng.loop.attachment_resolve import AttachmentResolver, materialize_history
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import Resume, Rewind
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import TURN_TERMINAL_KINDS, run_until_root_done

if TYPE_CHECKING:
    from pathlib import Path

CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image", "file"}), provider="sim", protocol="sim",
    tool_output_modalities=frozenset({"text", "image"}),
)
POLICY = ImageInputPolicy(
    enabled=True, max_images=4, max_item_bytes=4096, max_total_bytes=16384,
    allowed_media_types=frozenset({"image/png"}),
)
FILES = FileInputPolicy(enabled=True, max_files=2)


def _png(width: int = 3, height: int = 2) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big") + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


PNG = _png()
DIGEST = hashlib.sha256(PNG).hexdigest()
REF = f"sha256-{DIGEST}"
PDF = (b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n2 0 obj << /Type /Page >> endobj\n"
       b"trailer << >>\n%%EOF\n")


def _inline(data: bytes = PNG) -> dict[str, Any]:
    return ImageAttachmentV1.from_bytes(data, media_type="image/png").model_dump()


def _skill(root: Path, name: str, body: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(body, encoding="utf-8")


def _images(request: Any) -> list[ImagePart]:
    return [
        part for message in request.messages if isinstance(message.content, list)
        for part in message.content if isinstance(part, ImagePart)
    ]


def _files(request: Any) -> list[FilePart]:
    return [
        part for message in request.messages if isinstance(message.content, list)
        for part in message.content if isinstance(part, FilePart)
    ]


def _all_attachments(items: list[Any]) -> list[dict[str, Any]]:
    return [a for item in items for a in item.payload.get("attachments") or []]


def _frame_tool(data: bytes = PNG) -> ToolSpec:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("frame", attachments=(
            ImageAttachmentV1.from_bytes(data, media_type="image/png"),
        ))

    return ToolSpec(
        name="observe_frame", description="取一帧",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


async def _until_terminal(engine: Any, op: Any) -> list[Any]:
    """提交 op 并收集事件，直到出现任一 turn 终态（含挂起；共享 helper 不认挂起）。"""
    events: list[Any] = []
    holder: list[str] = []
    done = anyio.Event()

    async def collect() -> None:
        async for ev in engine.subscribe_all():
            if holder and ev.submission_id == holder[0]:
                events.append(ev)
                if ev.msg.kind in TURN_TERMINAL_KINDS:
                    done.set()
                    return

    async with anyio.create_task_group() as group:
        group.start_soon(collect)
        await anyio.sleep(0)
        holder.append(await engine.submit(op))
        with anyio.fail_after(10):
            await done.wait()
        group.cancel_scope.cancel()
    return events


async def _load(pool: taifeng.EnginePool, thread_id: str) -> list[Any]:
    return [item async for item in await pool.store.load_thread(thread_id)]


# ---------------------------------------------------------------------------
# 子 skill（call_skill）：子 runner 也拿到解析器
# ---------------------------------------------------------------------------

PLANNER = """---
name: planner
description: 规划者
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [watcher]
max_call_depth: 3
---
# 规划者
"""
WATCHER = """---
name: watcher
description: 取图观察 WATCHER_MARK
version: 1.0.0
type: composite
model: mock-model
tool_names: [observe_frame]
max_call_depth: 3
---
# 观察者 WATCHER_MARK
"""


async def test_child_skill_images_are_stored_and_resolved(tmp_path: Path, threads_dir: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills, "planner", PLANNER)
    _skill(skills, "watcher", WATCHER)
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[
        SimTurn(tool_calls=[{"id": "d", "name": "call_skill", "arguments":
            '{"reason": "看图", "skill_id": "watcher", "args": {"input": "看一帧"}}'}]),
        SimTurn(tool_calls=[{"id": "f1", "name": "observe_frame", "arguments": "{}"}]),
        SimTurn(text="看到了"),
        SimTurn(text="完成"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, extra_tools=[_frame_tool()], attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="planner")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="看一下"))
    parent = engine.thread_id
    threads = [meta.thread_id for meta in await pool.store.list_threads()]
    histories = {tid: await _load(pool, tid) for tid in threads}
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    (child,) = [tid for tid in threads if tid != parent]
    assert _all_attachments(histories[parent]) == []
    (stored,) = _all_attachments(histories[child])
    assert stored["encoding"] == "store" and stored["ref"] == REF
    # 子 thread 的第 2 次采样（拿到工具结果之后）里图片已被解析回来
    (image,) = _images(client.ledger.requests()[2].request)
    assert base64.b64decode(image.base64_data) == PNG


# ---------------------------------------------------------------------------
# 分离式 spawn：两个并发子 thread 写同一内容
# ---------------------------------------------------------------------------

HUB = """---
name: hub
description: 枢纽
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [watcher]
max_call_depth: 3
---
# 枢纽 HUB_MARK
"""


async def test_concurrent_spawns_share_one_object(tmp_path: Path, threads_dir: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills, "hub", HUB)
    _skill(skills, "watcher", WATCHER)
    store = LocalAttachmentStore(tmp_path / "blobs")
    script = [SimTurn(tool_calls=[{"id": "f", "name": "observe_frame", "arguments": "{}"}]),
              SimTurn(text="看到了")]
    client = RoutingSimClient(capabilities=CAPS, routes={
        "JOB-A": list(script), "JOB-B": list(script), "HUB_MARK": [SimTurn(text="ok")],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, extra_tools=[_frame_tool()], attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="hub")
    handles = [
        (await engine.spawn_skill(skill_id="watcher", args={"input": job}, reason="并发"))["handle_id"]
        for job in ("JOB-A", "JOB-B")
    ]
    for handle in handles:
        result = await engine.wait_spawn_terminal(
            handle_id=handle, timeout_seconds=10, cancel=CancellationToken(),
        )
        assert result["status"] == "done", result
    await pool.close()

    with_images = [r.request for r in client.ledger.requests() if _images(r.request)]
    assert len(with_images) == 2  # 两个子 thread 各自在第 2 次采样看到了图
    assert len(list(store.root.rglob(DIGEST))) == 1  # 内容寻址：只有一个对象


# ---------------------------------------------------------------------------
# 挂起 → Resume：恢复出来的 runner 也能解析
# ---------------------------------------------------------------------------

ASKER = """---
name: asker
description: 需要时向用户提问
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 提问者
"""


async def test_resume_after_suspension_still_sends_the_image(tmp_path: Path, threads_dir: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills, "asker", ASKER)
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[
        SimTurn(tool_calls=[{"id": "q1", "name": "request_user_input",
                             "arguments": '{"prompt": "这是哪一页？"}'}]),
        SimTurn(text="明白了"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, extra_tools=[make_request_user_input_tool()],
        attachment_store=store, attachment_cache_bytes=0,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="asker")
    first = await _until_terminal(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    assert first[-1].msg.kind == "turn_suspended", first[-1].msg.data
    record = SuspensionRecord.from_item(
        next(i for i in await _load(pool, engine.thread_id) if i.kind == "suspension"),
    )
    resumed = await run_until_root_done(engine, Resume(
        thread_id=engine.thread_id, resolutions={record.pending[0].request_id: {"answer": "第 3 页"}},
    ))
    await pool.close()

    assert resumed[-1].msg.kind == "turn_completed", resumed[-1].msg.data
    after_resume = client.ledger.requests()[-1].request
    (image,) = _images(after_resume)
    assert base64.b64decode(image.base64_data) == PNG


# ---------------------------------------------------------------------------
# Rewind re_reason：截断重推后的请求里图片仍在
# ---------------------------------------------------------------------------


async def test_rewind_redrive_keeps_the_image(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[
        SimTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        SimTurn(text="原收尾"),
        SimTurn(text="REDRIVE-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    node = next(n for n in engine.rewind_nodes() if n.kind == "iteration" and n.iteration_index == 2)
    events = await run_until_root_done(engine, Rewind(node_id=node.node_id, mode="re_reason"))
    await pool.close()

    assert "turn_rewound" in [e.msg.kind for e in events]
    redrive = client.ledger.requests()[-1].request
    (image,) = _images(redrive)
    assert base64.b64decode(image.base64_data) == PNG


# ---------------------------------------------------------------------------
# ContextEngine 视图：发的是视图，视图里的引用同样被解析
# ---------------------------------------------------------------------------


async def test_context_engine_view_is_resolved(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[SimTurn(text="1"), SimTurn(text="2")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, attachment_store=store,
        context_engine=TailWindowContextEngine(keep_last_turns=1),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="看图", attachments=[_inline()]))
    events = await run_until_root_done(engine, taifeng.UserMessage(text="再说说"))
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    # 第一条用户消息在视图里（任务起点）→ 第 2 次请求仍带这张图
    assert len(_images(client.ledger.requests()[1].request)) == 1


# ---------------------------------------------------------------------------
# 文件（PDF）附件
# ---------------------------------------------------------------------------


async def test_pdf_is_stored_with_page_count(skills_dir: Path, tmp_path: Path) -> None:
    pdf = FileAttachmentV1.from_bytes(PDF, filename="report.pdf").model_dump()
    requests = []
    for label, store in (("inline", None), ("store", LocalAttachmentStore(tmp_path / "blobs"))):
        client = SimClient(capabilities=CAPS, turns=[SimTurn(text="读完了")])
        threads = tmp_path / label
        threads.mkdir()
        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir, threads_dir=threads, model_client=client, compressors=[],
            file_input_policy=FILES, attachment_store=store,
        )
        engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
        events = await run_until_root_done(engine, taifeng.UserMessage(text="读一下", attachments=[pdf]))
        assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
        history = engine.history_snapshot()
        requests.append(client.ledger.single_request().request)
        await pool.close()
        if store is not None:
            (stored,) = _all_attachments(history)
            assert stored["encoding"] == "store" and stored["pages"] == 2
            assert stored["filename"] == "report.pdf"
            assert pdf["content"] not in "".join(
                p.read_text(encoding="utf-8") for p in threads.rglob("*.jsonl"))
    assert [m.model_dump() for m in requests[0].messages] == [m.model_dump() for m in requests[1].messages]
    assert len(_files(requests[1])) == 1


def test_pdf_reference_estimate_uses_pages() -> None:
    ref = FileAttachmentRefV1(
        ref="r1", media_type="application/pdf", size=len(PDF),
        sha256=hashlib.sha256(PDF).hexdigest(), pages=2,
    ).model_dump()
    unknown = {**ref, "pages": None}
    with_pages = estimate_history_tokens(
        [user_message("x", thread_id="t", attachments=[ref])], file_input_policy=FILES)
    without = estimate_history_tokens(
        [user_message("x", thread_id="t", attachments=[unknown])], file_input_policy=FILES)
    base = estimate_history_tokens([user_message("x", thread_id="t")], file_input_policy=FILES)
    assert with_pages - base == 2 * FILES.page_token_ceiling
    assert without - base == FILES.unknown_file_token_ceiling


# ---------------------------------------------------------------------------
# 混合形态、全有或全无、空附件、旧 thread 升级
# ---------------------------------------------------------------------------


async def test_inline_and_reference_in_one_message_keep_order(
    skills_dir: Path, threads_dir: Path, tmp_path: Path,
) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    second = _png(5, 4)
    ref = await store.put(second, media_type="image/png", sha256=hashlib.sha256(second).hexdigest())
    as_ref = ImageAttachmentRefV1(
        ref=ref, media_type="image/png", size=len(second),
        sha256=hashlib.sha256(second).hexdigest(), width=5, height=4,
    ).model_dump()
    client = SimClient(capabilities=CAPS, turns=[SimTurn(text="两张")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="两张图", attachments=[_inline(), as_ref]))
    await pool.close()

    first, after = _images(client.ledger.single_request().request)
    assert base64.b64decode(first.base64_data) == PNG
    assert base64.b64decode(after.base64_data) == second


async def test_one_bad_attachment_rejects_the_whole_message(
    skills_dir: Path, threads_dir: Path, tmp_path: Path,
) -> None:
    """第二个附件取不回：整条消息被拒，第一个附件也不写入存储。"""
    store = LocalAttachmentStore(tmp_path / "blobs")
    missing = ImageAttachmentRefV1(
        ref="sha256-" + "d" * 64, media_type="image/png", size=len(PNG), sha256="d" * 64,
        width=3, height=2,
    ).model_dump()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(capabilities=CAPS, turns=[]),
        compressors=[], image_input_policy=POLICY, attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    with pytest.raises(AttachmentUnavailableError):
        await engine.submit(taifeng.UserMessage(text="两张图", attachments=[_inline(), missing]))
    await pool.close()
    assert not store.root.exists() or list(store.root.rglob("*")) == []


async def test_message_without_attachments_touches_nothing(
    skills_dir: Path, threads_dir: Path, tmp_path: Path,
) -> None:
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[SimTurn(text="好")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, attachment_store=store,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    events = await run_until_root_done(engine, taifeng.UserMessage(text="你好"))
    await pool.close()
    assert events[-1].msg.kind == "turn_completed"
    assert not [e for e in events if e.msg.kind.startswith("attachment_")]
    assert not store.root.exists()


async def test_old_inline_thread_upgrades_to_mixed(skills_dir: Path, threads_dir: Path, tmp_path: Path) -> None:
    """先无存储写下内联图片，再以有存储的 pool 恢复：旧图仍内联发送，新图落引用。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[], image_input_policy=POLICY,
        model_client=SimClient(capabilities=CAPS, turns=[SimTurn(text="1")]),
    )
    engine = await pool.get_or_create(session_id="old", entry_skill_id="code-reviewer")
    await run_until_root_done(engine, taifeng.UserMessage(text="旧图", attachments=[_inline()]))
    thread_id = engine.thread_id
    await pool.close()

    second = _png(7, 7)
    client = SimClient(capabilities=CAPS, turns=[SimTurn(text="2")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, compressors=[], image_input_policy=POLICY,
        model_client=client, attachment_store=LocalAttachmentStore(tmp_path / "blobs"),
    )
    engine = await pool.get_or_create(
        session_id="new", entry_skill_id="code-reviewer", resume_thread_id=thread_id,
    )
    events = await run_until_root_done(engine, taifeng.UserMessage(text="新图", attachments=[_inline(second)]))
    history = engine.history_snapshot()
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
    encodings = [a["encoding"] for a in _all_attachments(history)]
    assert encodings == ["base64", "store"]
    old, new = _images(client.ledger.single_request().request)
    assert base64.b64decode(old.base64_data) == PNG
    assert base64.b64decode(new.base64_data) == second


# ---------------------------------------------------------------------------
# handoff 摘要：指针原样保留，之后仍可读回
# ---------------------------------------------------------------------------


async def test_handoff_keeps_reference_pointer() -> None:
    as_ref = ImageAttachmentRefV1(
        ref=REF, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    ).model_dump()
    history = [user_message("看图", thread_id="t", attachments=[as_ref]),
               assistant_message("好", thread_id="t", model="m"),
               user_message("继续", thread_id="t"), assistant_message("嗯", thread_id="t", model="m"),
               user_message("再继续", thread_id="t"), assistant_message("收尾", thread_id="t", model="m")]
    budget = ContextBudget(context_window=1000, preserve_tail_messages=2)
    result = await HandoffCompactionStrategy(
        model_client=SimClient(turns=[SimTurn(text="## 进度\n- 看过图")]), model="m",
    ).compress(
        CompressionContext(
            history=history, token_estimate=900, budget=budget, cache_anchor_index=-1,
            phase="pre_turn",
            available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
        ),
        InitialContextInjection.BEFORE_LAST_USER_MESSAGE,
    )
    assert result.success, result.reason
    (summary,) = [i for i in result.new_history if i.kind == "compacted"]
    assert f"path=attachment:{REF}" in summary.payload["summary"]
    assert collect_attachment_refs(result.new_history) == {REF}


# ---------------------------------------------------------------------------
# 解析器边界：LRU、超大对象、后端异常、取消
# ---------------------------------------------------------------------------


class _Counting(LocalAttachmentStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gets = 0

    async def get(self, ref: str) -> bytes:
        self.gets += 1
        return await super().get(ref)


async def test_resolver_lru_evicts_oldest_and_skips_oversized(tmp_path: Path) -> None:
    store = _Counting(tmp_path)
    blobs = [_png(i + 1, 1) for i in range(3)]
    refs = [await store.put(b, media_type="image/png", sha256=hashlib.sha256(b).hexdigest()) for b in blobs]
    resolver = AttachmentResolver(store, cache_bytes=len(blobs[0]) * 2)

    async def fetch(i: int) -> None:
        await resolver.fetch(refs[i], sha256=hashlib.sha256(blobs[i]).hexdigest(), size=len(blobs[i]))

    await fetch(0)
    await fetch(1)
    await fetch(2)          # 容量两份：最久未用的 0 被淘汰
    await fetch(1)          # 命中
    assert store.gets == 3
    await fetch(0)          # 被淘汰过 → 再读一次
    assert store.gets == 4

    tiny = AttachmentResolver(store, cache_bytes=len(blobs[0]) - 1)
    for _ in range(2):
        await tiny.fetch(refs[0], sha256=hashlib.sha256(blobs[0]).hexdigest(), size=len(blobs[0]))
    assert store.gets == 6  # 比容量大的对象从不进缓存


class _Broken(LocalAttachmentStore):
    async def get(self, ref: str) -> bytes:
        raise RuntimeError("backend unreachable")


async def test_backend_error_degrades_with_its_own_reason(tmp_path: Path) -> None:
    as_ref = ImageAttachmentRefV1(
        ref=REF, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    ).model_dump()
    seen: list[Any] = []

    async def emit(msg: Any) -> None:
        seen.append(msg)

    items = [user_message("看图", thread_id="t", attachments=[as_ref])]
    out = await materialize_history(items, AttachmentResolver(_Broken(tmp_path)), emit=emit)
    assert out[0].payload["attachments"] == []
    assert "[attachment unavailable:" in out[0].payload["text"] and "backend_error" in out[0].payload["text"]
    assert [m.data["reason"] for m in seen] == ["backend_error"]
    assert items[0].payload["attachments"] == [as_ref]  # 入参未被修改


class _Slow(LocalAttachmentStore):
    async def get(self, ref: str) -> bytes:
        await anyio.sleep(30)
        raise AssertionError("unreachable")


async def test_cancellation_during_resolve_cleans_up(tmp_path: Path) -> None:
    as_ref = ImageAttachmentRefV1(
        ref=REF, media_type="image/png", size=len(PNG), sha256=DIGEST, width=3, height=2,
    ).model_dump()
    resolver = AttachmentResolver(_Slow(tmp_path))
    with anyio.move_on_after(0.05) as scope:
        await materialize_history([user_message("x", thread_id="t", attachments=[as_ref])], resolver)
    assert scope.cancelled_caught  # 取消没有被当成取不回吞掉
    assert resolver._inflight == {}  # noqa: SLF001 —— 并发合并的登记已清理


async def test_store_error_types_are_one_family(tmp_path: Path) -> None:
    """存储层错误同一基类，宿主可以一处捕获。"""
    from taifeng.conversation.attachment_store import (
        AttachmentIntegrityError,
        AttachmentNotFoundError,
    )

    assert issubclass(AttachmentNotFoundError, AttachmentStoreError)
    assert issubclass(AttachmentIntegrityError, AttachmentStoreError)
    with pytest.raises(AttachmentStoreError):
        await LocalAttachmentStore(tmp_path).get("sha256-" + "e" * 64)


# ---------------------------------------------------------------------------
# 子 skill 挂起 → Resume(子 thread)：续跑链重建的子 runner 也能解析
# ---------------------------------------------------------------------------

ASKING_WATCHER = """---
name: watcher
description: 取图并在需要时提问
version: 1.0.0
type: composite
model: mock-model
tool_names: [observe_frame, request_user_input]
max_call_depth: 3
---
# 观察者
"""


async def test_child_resume_chain_still_sends_child_image(tmp_path: Path, threads_dir: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills, "planner", PLANNER)
    _skill(skills, "watcher", ASKING_WATCHER)
    store = LocalAttachmentStore(tmp_path / "blobs")
    client = SimClient(capabilities=CAPS, turns=[
        SimTurn(tool_calls=[{"id": "d", "name": "call_skill", "arguments":
            '{"reason": "看图", "skill_id": "watcher", "args": {"input": "看一帧"}}'}]),
        SimTurn(tool_calls=[{"id": "f1", "name": "observe_frame", "arguments": "{}"}]),
        SimTurn(tool_calls=[{"id": "q1", "name": "request_user_input",
                             "arguments": '{"prompt": "要放大吗？"}'}]),
        SimTurn(text="子结论"),
        SimTurn(text="父收尾"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        image_input_policy=POLICY, attachment_store=store, attachment_cache_bytes=0,
        extra_tools=[_frame_tool(), make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="planner")
    first = await _until_terminal(engine, taifeng.UserMessage(text="看一下"))
    assert first[-1].msg.kind == "turn_suspended", first[-1].msg.data
    parent = engine.thread_id
    (child,) = [m.thread_id for m in await pool.store.list_threads() if m.thread_id != parent]
    record = SuspensionRecord.from_item(
        next(i for i in await _load(pool, child) if i.kind == "suspension"),
    )
    resumed = await run_until_root_done(engine, Resume(
        thread_id=child, resolutions={record.pending[0].request_id: {"answer": "不用"}},
    ))
    await pool.close()

    assert resumed[-1].msg.kind == "turn_completed", resumed[-1].msg.data
    child_after_resume = client.ledger.requests()[3].request
    (image,) = _images(child_after_resume)
    assert base64.b64decode(image.base64_data) == PNG
