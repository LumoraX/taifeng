"""EnginePool 文件策略 → admission → prompt → provider 的完整注入链（真实 pool + SimClient）。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.models import user_message
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import FileCountExceededError, UnsupportedModalityError
from taifeng.llm.file_input import FileInputPolicy
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import FilePart, ImagePart, TextPart
from taifeng.loop.audit_admission import prepare_user_message
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.prompt import history_to_api_messages
from taifeng.loop.submission import Submission, UserMessage
from taifeng.loop.turn import TurnRunner
from tests.pdf_fixtures import pdf_attachment

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.loop.engine import AgentEngine

_FILE_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "file"}), provider="sim", protocol="sim"
)
_ALL_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image", "file"}), provider="sim", protocol="sim"
)
_POLICY = FileInputPolicy(enabled=True, max_files=2, max_item_bytes=64 * 1024,
                          max_total_bytes=128 * 1024)
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
)


async def _pool(
    skills_dir: Path, threads_dir: Path, client: SimClient, **kwargs: Any
) -> taifeng.EnginePool:
    """带文件策略（可覆盖）的真实 EnginePool。"""
    kwargs.setdefault("file_input_policy", _POLICY)
    return await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        **kwargs,
    )


async def _run(engine: AgentEngine, op: UserMessage) -> tuple[str, dict[str, Any]]:
    """提交并等到 turn 终态，返回（终态 kind, 事件 data）。"""
    submission_id = await engine.submit(op)
    async for event in engine.subscribe(submission_id):
        if event.msg.kind in ("turn_completed", "turn_failed"):
            return event.msg.kind, dict(event.msg.data or {})
    raise AssertionError("turn never terminated")


async def test_pool_injects_enabled_file_policy_into_request(
    skills_dir: Path, threads_dir: Path
) -> None:
    """业务启用策略 + client 声明 file：请求里是 [TextPart, FilePart]，正文逐位透传。"""
    client = SimClient(turns=[SimTurn(text="seen")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, client)
    engine = await pool.get_or_create(session_id="file", entry_skill_id="code-reviewer")
    attachment = pdf_attachment("hello")

    kind, _ = await _run(engine, taifeng.UserMessage(text="read", attachments=[attachment]))
    recorded = client.ledger.single_request()
    await pool.close()

    assert kind == "turn_completed"
    content = recorded.request.messages[-1].content
    assert isinstance(content, list)
    assert isinstance(content[0], TextPart)
    assert isinstance(content[1], FilePart)
    assert content[1].base64_data == attachment["content"]
    (descriptor,) = recorded.file_inputs()
    assert (descriptor.filename, descriptor.sha256) == ("note.pdf", attachment["sha256"])


async def test_mixed_image_and_file_attachments_keep_submission_order(
    skills_dir: Path, threads_dir: Path
) -> None:
    """图片与文件交错提交时，parts 顺序与 attachments 顺序一致（文字仍在首项）。"""
    client = SimClient(turns=[SimTurn(text="seen")], capabilities=_ALL_CAPS)
    pool = await _pool(
        skills_dir, threads_dir, client,
        image_input_policy=ImageInputPolicy(
            enabled=True, max_item_bytes=1024, max_total_bytes=1024,
            allowed_media_types=frozenset({"image/png"}),
        ),
    )
    engine = await pool.get_or_create(session_id="mixed", entry_skill_id="code-reviewer")
    image = ImageAttachmentV1.from_bytes(_PNG, media_type="image/png").model_dump()
    attachments = [pdf_attachment("a"), image, pdf_attachment("b", filename="b.pdf")]

    kind, _ = await _run(engine, taifeng.UserMessage(text="all", attachments=attachments))
    request = client.ledger.single_request().request
    await pool.close()

    assert kind == "turn_completed"
    content = request.messages[-1].content
    assert isinstance(content, list)
    assert [type(part) for part in content] == [TextPart, FilePart, ImagePart, FilePart]
    assert [part.filename for part in content if isinstance(part, FilePart)] == [
        "note.pdf", "b.pdf",
    ]


async def test_disabled_policy_rejects_file_before_conversation_append(
    skills_dir: Path, threads_dir: Path
) -> None:
    """默认策略关闭：submit 即抛 unsupported_modality，不留会在每次恢复时报错的脏历史。"""
    client = SimClient(turns=[SimTurn(text="must not run")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, client, file_input_policy=None)
    engine = await pool.get_or_create(session_id="off", entry_skill_id="code-reviewer")

    with pytest.raises(UnsupportedModalityError, match="disabled by policy"):
        await engine.submit(taifeng.UserMessage(text="read", attachments=[pdf_attachment()]))

    items = [item async for item in await pool.store.load_thread(engine.thread_id)]
    await pool.close()
    assert items == []
    assert client.ledger.requests() == []


async def test_client_without_file_capability_rejects_before_append(
    skills_dir: Path, threads_dir: Path
) -> None:
    """策略开了但 client 未声明 file：能力门先拒，绝不静默降级为文件名文本。"""
    client = SimClient(turns=[SimTurn(text="must not run")])
    pool = await _pool(skills_dir, threads_dir, client)
    engine = await pool.get_or_create(session_id="caps", entry_skill_id="code-reviewer")

    with pytest.raises(UnsupportedModalityError, match="does not support file input"):
        await engine.submit(taifeng.UserMessage(text="read", attachments=[pdf_attachment()]))

    items = [item async for item in await pool.store.load_thread(engine.thread_id)]
    await pool.close()
    assert items == []
    assert client.ledger.requests() == []


async def test_policy_limits_are_enforced_at_submit(
    skills_dir: Path, threads_dir: Path
) -> None:
    client = SimClient(turns=[], capabilities=_FILE_CAPS)
    pool = await _pool(
        skills_dir, threads_dir, client, file_input_policy=FileInputPolicy(enabled=True)
    )
    engine = await pool.get_or_create(session_id="limits", entry_skill_id="code-reviewer")

    with pytest.raises(FileCountExceededError):
        await engine.submit(
            taifeng.UserMessage(text="x", attachments=[pdf_attachment(), pdf_attachment()])
        )
    await pool.close()


async def test_request_capture_redacts_file_body(skills_dir: Path, threads_dir: Path) -> None:
    """request capture 只能记录文件描述（MIME / size / sha256 / filename），不复制正文。"""
    client = SimClient(turns=[SimTurn(text="seen")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, client, enable_request_capture=True)
    engine = await pool.get_or_create(session_id="capture", entry_skill_id="code-reviewer")
    attachment = pdf_attachment("capture")

    submission_id = await engine.submit(
        taifeng.UserMessage(text="read", attachments=[attachment])
    )
    capture: dict[str, Any] | None = None
    async for event in engine.subscribe(submission_id):
        if event.msg.kind == "llm_request_recorded":
            capture = event.msg.data
        if event.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()

    assert capture is not None
    encoded = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    assert attachment["content"] not in encoded
    assert "base64_data" not in encoded
    assert '"content_redacted": true' in encoded
    assert attachment["sha256"] in encoded


async def test_cold_resume_replays_file_from_jsonl(skills_dir: Path, threads_dir: Path) -> None:
    """JSONL 往返：重开 pool 后 attachment 原样读回，下一轮请求再次带上同一文件。"""
    attachment = pdf_attachment("persist")
    first = SimClient(turns=[SimTurn(text="seen")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, first)
    engine = await pool.get_or_create(session_id="hot", entry_skill_id="code-reviewer")
    kind, _ = await _run(engine, taifeng.UserMessage(text="read", attachments=[attachment]))
    thread_id = engine.thread_id
    await pool.close()
    assert kind == "turn_completed"

    second = SimClient(turns=[SimTurn(text="again")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, second)
    stored = [item async for item in await pool.store.load_thread(thread_id)]
    engine = await pool.get_or_create(
        session_id="cold", entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )
    kind, _ = await _run(engine, taifeng.UserMessage(text="what was in it?"))
    recorded = second.ledger.single_request()
    await pool.close()

    users = [item for item in stored if item.kind == "user_message"]
    assert users[0].payload["attachments"] == [attachment]
    assert kind == "turn_completed"
    assert [d.sha256 for d in recorded.file_inputs()] == [attachment["sha256"]]


async def test_cold_resume_with_policy_disabled_fails_loudly(
    skills_dir: Path, threads_dir: Path
) -> None:
    """重开时关了文件策略：含文件的历史不得被静默丢弃，turn 以 unsupported_modality 失败。"""
    first = SimClient(turns=[SimTurn(text="seen")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, first)
    engine = await pool.get_or_create(session_id="hot2", entry_skill_id="code-reviewer")
    await _run(engine, taifeng.UserMessage(text="read", attachments=[pdf_attachment()]))
    thread_id = engine.thread_id
    await pool.close()

    second = SimClient(turns=[SimTurn(text="must not run")], capabilities=_FILE_CAPS)
    pool = await _pool(skills_dir, threads_dir, second, file_input_policy=None)
    engine = await pool.get_or_create(
        session_id="cold2", entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )
    kind, data = await _run(engine, taifeng.UserMessage(text="again"))
    await pool.close()

    assert kind == "turn_failed"
    assert "file input is disabled by policy" in json.dumps(data, ensure_ascii=False)
    assert second.ledger.requests() == []


async def test_engine_estimate_tokens_counts_file_pages(
    skills_dir: Path, threads_dir: Path
) -> None:
    """公共 estimate_tokens 与 turn preflight 共用文件策略：按页数 × 每页上界计量。"""
    policy = FileInputPolicy(enabled=True, page_token_ceiling=1000)
    pool = await _pool(
        skills_dir, threads_dir, SimClient(turns=[], capabilities=_FILE_CAPS),
        file_input_policy=policy,
    )
    engine = await pool.get_or_create(session_id="estimate", entry_skill_id="code-reviewer")
    baseline = engine.estimate_tokens()
    engine._history.append(  # noqa: SLF001
        user_message("x", thread_id=engine.thread_id, attachments=[pdf_attachment(pages=3)])
    )

    estimated = engine.estimate_tokens()
    await pool.close()

    assert estimated - baseline >= 3000


async def test_detached_and_resumed_child_runners_inherit_file_policy(
    skills_dir: Path, threads_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """detached spawn 子 runner 与非根 thread 续跑 runner 都不得退回默认关闭策略。"""
    pool = await _pool(skills_dir, threads_dir, SimClient(turns=[], capabilities=_FILE_CAPS))
    engine = await pool.get_or_create(session_id="child", entry_skill_id="code-reviewer")
    target = engine._snapshot.get("style-checker")  # noqa: SLF001
    assert target is not None
    child_thread_id = await pool.store.create_thread(
        cwd=None, entry_skill_id=target.id, source="test"
    )
    runner = engine._build_child_runner(  # noqa: SLF001
        target, child_thread_id, user_message("x", thread_id=child_thread_id),
        CancellationToken(),
    )
    await pool.store.append(user_message("inspect", thread_id=child_thread_id))
    captured: list[TurnRunner] = []

    async def fake_run(self: TurnRunner) -> object:
        captured.append(self)
        return object()

    monkeypatch.setattr(TurnRunner, "run", fake_run)
    await engine._run_thread_turn(  # noqa: SLF001
        Submission(op=UserMessage(text="resume")), child_thread_id, "style-checker",
        CancellationToken(),
    )
    await pool.close()

    assert runner.file_input_policy is _POLICY
    assert captured[0].file_input_policy is _POLICY


def test_history_projection_requires_policy_and_capability() -> None:
    """prompt 重建与入队准入同一道门：缺策略 / 缺能力都抛错，不静默跳过附件。"""
    item = user_message("x", thread_id="t", attachments=[pdf_attachment()])

    with pytest.raises(UnsupportedModalityError, match="does not support file input"):
        history_to_api_messages([item], file_input_policy=_POLICY)
    with pytest.raises(UnsupportedModalityError, match="disabled by policy"):
        history_to_api_messages([item], model_capabilities=_FILE_CAPS)
    (message,) = history_to_api_messages(
        [item], file_input_policy=_POLICY, model_capabilities=_FILE_CAPS
    )
    assert isinstance(message.content, list)
    assert isinstance(message.content[1], FilePart)


def test_strict_audit_admission_rejects_file_attachment() -> None:
    """strict Journal 的 AttachmentV1 只有图片形状：文件在 acceptance 前显式拒绝。"""
    state = SimpleNamespace(
        thread_id="t", max_attachment_bytes=1 << 20, max_total_attachment_bytes=1 << 20
    )
    submission = Submission(op=UserMessage(text="x", attachments=[pdf_attachment()]))

    with pytest.raises(UnsupportedModalityError, match="strict audit journal"):
        prepare_user_message(state, submission)  # type: ignore[arg-type]


async def test_strict_audit_file_submission_is_durably_rejected(
    tmp_path: Path, skills_dir: Path
) -> None:
    """真实 strict audit engine：只落脱敏 submission_rejected，不写 conversation item。"""
    from taifeng.loop.audit_admission import InvalidAuditedSubmissionError
    from tests.loop.test_audit_submission_admission import _engine_with_audit

    client = SimClient(turns=[], capabilities=_FILE_CAPS)
    engine, _, core = await _engine_with_audit(tmp_path, skills_dir, model_client=client)
    attachment = pdf_attachment()

    with pytest.raises(InvalidAuditedSubmissionError):
        await engine.submit(UserMessage(text="read", attachments=[attachment]))

    committed = [envelope async for envelope in core.load("ses_audit_submission")]
    assert committed[-1].record_type == "submission_rejected"
    assert not any(envelope.record_type == "conversation_item" for envelope in committed)
    assert attachment["content"] not in repr(committed)
    assert engine._history == []  # noqa: SLF001
