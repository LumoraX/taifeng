"""审计模式下的文件附件与工具图片附件（ADR 0095）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal import JournalHealth
from taifeng.conversation.journal.attachment_records import FileAttachmentRecordV1
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.records import AttachmentV1, SubmissionAcceptedV1
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.file_input import FileInputPolicy
from taifeng.llm.image_input import ImageAttachmentV1, ImageInputPolicy
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_admission import InvalidAuditedSubmissionError
from taifeng.loop.audit_bootstrap import AuditSessionReleaseError
from taifeng.loop.audit_config import AuditConfig
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import run_until_root_done_kind
from tests.pdf_fixtures import pdf_attachment

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.conversation.journal.models import JournalEnvelope

_SESSION = "ses-attachments"
_CAPS = ModelCapabilities(
    input_modalities=frozenset({"text", "image", "file"}),
    tool_output_modalities=frozenset({"text", "image"}),
    provider="sim", protocol="sim",
)
_SKILL = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [screenshot]
max_call_depth: 2
---
# 入口
"""
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
    + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True, exist_ok=True)
    (root / "entry" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return root


def _screenshot() -> ToolSpec:
    """返回一张图片的只读工具。"""

    async def handler(args: dict[str, Any], ctx: object) -> ToolResult:
        return ToolResult.ok(
            "已截图",
            attachments=(ImageAttachmentV1.from_bytes(_PNG, media_type="image/png"),),
        )

    return ToolSpec(
        name="screenshot", description="截图",
        input_schema={"type": "object", "properties": {}},
        handler=handler, effect_kind="pure", reconciliation="none",
    )


class _Run:
    """一个审计 Session。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.sim: SimClient
        self.core: JsonlSessionJournalCore

    async def start(
        self, turns: list[SimTurn], *, resume_thread_id: str | None = None, **kwargs: Any,
    ) -> None:
        self.sim = SimClient(turns=turns, capabilities=_CAPS)
        self.core = JsonlSessionJournalCore(self.tmp_path / "journal")
        kwargs.setdefault("file_input_policy", FileInputPolicy(enabled=True))
        kwargs.setdefault("image_input_policy", ImageInputPolicy(enabled=True))
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(self.tmp_path),
            threads_dir=self.tmp_path / "threads",
            model_client=AttemptObservableClientAdapter(
                self.sim, provider="sim", default_model="sim-model"
            ),
            compressors=[],
            extra_tools=[_screenshot()],
            audit=AuditConfig(
                journal_core=self.core, writer_id="writer-attachments",
                max_attachment_bytes=65536, max_total_attachment_bytes=1048576,
            ),
            **kwargs,
        )
        self.engine = await self.pool.get_or_create(
            session_id=_SESSION, entry_skill_id="entry", resume_thread_id=resume_thread_id,
        )

    async def journal(self) -> list[JournalEnvelope]:
        core = JsonlSessionJournalCore(self.tmp_path / "journal")
        return [envelope async for envelope in core.load(_SESSION)]


def _of(envelopes: list[JournalEnvelope], record_type: str) -> list[JournalEnvelope]:
    return [e for e in envelopes if e.record_type == record_type]


# ====================================================================
# 用户消息里的文件
# ====================================================================


async def test_file_attachment_is_accepted_and_journaled(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([SimTurn(text="读过了")])
    attachment = pdf_attachment(filename="报告.pdf")

    kind = await run_until_root_done_kind(
        run.engine, taifeng.UserMessage(text="请阅读", attachments=[attachment]),
    )

    assert kind == "turn_completed"
    envelopes = await run.journal()
    accepted = SubmissionAcceptedV1.model_validate(_of(envelopes, "submission_accepted")[0].payload)
    assert accepted.attachments is not None
    (record,) = accepted.attachments
    assert type(record) is FileAttachmentRecordV1
    assert (record.filename, record.media_type) == ("报告.pdf", "application/pdf")
    assert record.decoded().startswith(b"%PDF")
    # 对话项里的附件与非审计路径的形状逐项相同
    user_item = next(i for i in run.engine.history_snapshot() if i.kind == "user_message")
    assert user_item.payload["attachments"] == [attachment]
    # 模型确实收到了文件
    sent = run.sim.ledger.requests()[0]
    assert [f.filename for f in sent.file_inputs()] == ["报告.pdf"]
    await run.pool.close()


async def test_file_body_is_redacted_from_the_request_record(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([SimTurn(text="读过了")])
    attachment = pdf_attachment()

    await run_until_root_done_kind(
        run.engine, taifeng.UserMessage(text="请阅读", attachments=[attachment]),
    )

    (request,) = _of(await run.journal(), "llm_request_committed")
    # 请求的 messages 与 input_items 两个视图各有一份正文，都被脱敏
    assert {r["kind"] for r in request.payload["redactions"]} == {"file_base64"}
    assert attachment["content"] not in repr(request.payload)
    assert attachment["sha256"] in repr(request.payload["api_request_safe"])
    await run.pool.close()


async def test_image_and_file_in_one_message(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([SimTurn(text="都看了")])
    image = ImageAttachmentV1.from_bytes(_PNG, media_type="image/png").model_dump()
    file = pdf_attachment(filename=None)

    kind = await run_until_root_done_kind(
        run.engine, taifeng.UserMessage(text="请看", attachments=[image, file]),
    )

    assert kind == "turn_completed"
    accepted = SubmissionAcceptedV1.model_validate(
        _of(await run.journal(), "submission_accepted")[0].payload
    )
    assert accepted.attachments is not None
    assert [type(a) for a in accepted.attachments] == [AttachmentV1, FileAttachmentRecordV1]
    user_item = next(i for i in run.engine.history_snapshot() if i.kind == "user_message")
    assert user_item.payload["attachments"] == [image, file]
    await run.pool.close()


async def test_resume_restores_the_file_attachment(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([SimTurn(text="读过了")])
    attachment = pdf_attachment()
    await run_until_root_done_kind(
        run.engine, taifeng.UserMessage(text="请阅读", attachments=[attachment]),
    )
    before = list(run.engine.history_snapshot())
    thread_id = run.engine.thread_id
    await run.core.close()
    with pytest.raises(AuditSessionReleaseError):
        await run.pool.close()

    resumed = _Run(tmp_path)
    await resumed.start([SimTurn(text="还记得")], resume_thread_id=thread_id)

    assert list(resumed.engine.history_snapshot()) == before
    assert await run_until_root_done_kind(
        resumed.engine, taifeng.UserMessage(text="文件里写了什么"),
    ) == "turn_completed"
    # 续跑的请求里仍带着那份文件
    assert len(resumed.sim.ledger.requests()[-1].file_inputs()) == 1
    await resumed.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"file_input_policy": FileInputPolicy(enabled=False)},
        {"file_input_policy": FileInputPolicy(enabled=True, max_item_bytes=16)},
    ],
)
async def test_inadmissible_file_is_durably_rejected(
    tmp_path: Path, kwargs: dict[str, Any],
) -> None:
    run = _Run(tmp_path)
    await run.start([], **kwargs)
    attachment = pdf_attachment()

    with pytest.raises(InvalidAuditedSubmissionError):
        await run.engine.submit(taifeng.UserMessage(text="请阅读", attachments=[attachment]))

    envelopes = await run.journal()
    assert envelopes[-1].record_type == "submission_rejected"
    assert _of(envelopes, "submission_accepted") == []
    assert attachment["content"] not in repr([e.payload for e in envelopes])
    assert list(run.engine.history_snapshot()) == []
    await run.pool.close()


@pytest.mark.parametrize(
    "tamper",
    [
        {"filename": "../etc/passwd"},
        {"sha256": "0" * 64},
        {"size": 1},
        {"content": "data:application/pdf;base64,AAAA"},
        {"media_type": "text/plain"},
    ],
)
async def test_malformed_file_is_durably_rejected(
    tmp_path: Path, tamper: dict[str, Any],
) -> None:
    run = _Run(tmp_path)
    await run.start([])

    with pytest.raises(InvalidAuditedSubmissionError):
        await run.engine.submit(taifeng.UserMessage(
            text="请阅读", attachments=[{**pdf_attachment(), **tamper}],
        ))

    assert (await run.journal())[-1].record_type == "submission_rejected"
    await run.pool.close()


# ====================================================================
# 工具结果里的图片
# ====================================================================


async def test_tool_image_reaches_the_model_and_the_journal(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start([
        SimTurn(text="截个图", tool_calls=[
            {"id": "call-1", "name": "screenshot", "arguments": "{}"},
        ]),
        SimTurn(text="看到了"),
    ])

    kind = await run_until_root_done_kind(run.engine, taifeng.UserMessage(text="看看屏幕"))

    assert kind == "turn_completed"
    envelopes = await run.journal()
    (outcome,) = _of(envelopes, "tool_outcome_committed")
    assert outcome.payload["status"] == "success"
    assert [a["media_type"] for a in outcome.payload["data"]["attachments"]] == ["image/png"]
    output = next(
        i for i in run.engine.history_snapshot() if i.kind == "function_call_output"
    )
    assert len(output.payload["attachments"]) == 1
    # 第二次采样的请求带着图片，落账时正文被脱敏
    assert len(run.sim.ledger.requests()[-1].image_inputs()) == 1
    second = _of(envelopes, "llm_request_committed")[-1]
    assert {r["kind"] for r in second.payload["redactions"]} == {"image_base64"}
    await run.pool.close()
    verification = await JsonlSessionJournalCore(tmp_path / "journal").verify(_SESSION)
    assert verification.health is JournalHealth.HEALTHY


async def test_tool_image_without_policy_becomes_an_error_result(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    await run.start(
        [
            SimTurn(text="截个图", tool_calls=[
                {"id": "call-1", "name": "screenshot", "arguments": "{}"},
            ]),
            SimTurn(text="看不到"),
        ],
        image_input_policy=ImageInputPolicy(enabled=False),
    )

    kind = await run_until_root_done_kind(run.engine, taifeng.UserMessage(text="看看屏幕"))

    assert kind == "turn_completed"
    output = run.sim.ledger.function_call_output_text("call-1") or ""
    assert output.startswith("tool_attachment_rejected:")
    (outcome,) = _of(await run.journal(), "tool_outcome_committed")
    assert outcome.payload["status"] == "error"
    await run.pool.close()


# ====================================================================
# 记录形状
# ====================================================================


def test_accepted_attachments_are_discriminated_by_kind() -> None:
    image = ImageAttachmentV1.from_bytes(_PNG, media_type="image/png").model_dump()
    file = pdf_attachment()

    accepted = SubmissionAcceptedV1(
        op_kind="user_message", turn_index=0, text="x", source="user",
        attachments=[image, file],  # type: ignore[arg-type]
    )

    assert accepted.attachments is not None
    assert [type(a) for a in accepted.attachments] == [AttachmentV1, FileAttachmentRecordV1]
    restored = SubmissionAcceptedV1.model_validate(accepted.model_dump(mode="json"))
    assert restored == accepted


def test_image_attachment_bytes_are_unchanged() -> None:
    """图片附件的 durable 形状没有因为引入文件附件而多出字段。"""
    image = ImageAttachmentV1.from_bytes(_PNG, media_type="image/png").model_dump()

    record = AttachmentV1.model_validate(image)

    assert "filename" not in record.model_dump()
