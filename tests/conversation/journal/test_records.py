"""SessionJournal V1 业务 record 契约测试。"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import taifeng.conversation as conversation
from taifeng.conversation.journal import ActorRef, JournalConflictError
from taifeng.conversation.journal.canonical import (
    canonical_bytes,
    model_canonical_data,
    record_fingerprint,
)
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.conversation.journal.models import (
    RootThreadDescriptor,
    SessionDescriptor,
    build_initialization_records,
)
from taifeng.conversation.journal.records import (
    AttachmentV1,
    ConversationItemV1,
    JournalIdentities,
    JournalRecordFactory,
    LlmRequestCommittedV1,
    LlmResponseCheckpointV1,
    LlmResponseCommittedV1,
    LlmStatus,
    SessionEndedV1,
    SkillDispatchFinishedV1,
    SkillDispatchStartedV1,
    SkillSelectedV1,
    SkillStatus,
    StableErrorV1,
    SubmissionAcceptedV1,
    SubmissionAppliedV1,
    SubmissionRejectedV1,
    ThreadBoundV1,
    ThreadCreatedV1,
    ThreadTerminalV1,
    ToolIntentCommittedV1,
    ToolOutcomeCommittedV1,
    ToolStatus,
    TurnCancelledV1,
    TurnCompletedV1,
    TurnFailedV1,
    TurnStartedV1,
    UnsupportedConversationItemError,
    conversation_item_record,
    deserialize_response_item,
    record_id,
    serialize_response_item,
    stable_error,
    validate_attachments,
)
from taifeng.conversation.models import ResponseItem
from taifeng.llm.errors import InvalidRequestError
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from pathlib import Path


def _stable_error() -> StableErrorV1:
    """构造测试用的稳定错误。"""
    return StableErrorV1(
        code="invalid_input",
        class_name="InputError",
        failure_class="invalid_request",
        retryable=False,
    )


def _payload_examples() -> list[object]:
    """覆盖所有 V1 payload DTO 的最小合法形状。"""
    error = _stable_error()
    return [
        SubmissionAcceptedV1(
            op_kind="user_message", text="hello", attachments=(), source="cli"
        ),
        SubmissionAppliedV1(
            accepted_record_id="accepted_1",
            result_status="applied",
            conversation_item_ids=("item_1",),
            terminal_record_ids=("terminal_1",),
        ),
        SubmissionRejectedV1(
            op_kind="unsupported", stable_error=error, input_descriptor_hash="a" * 64
        ),
        TurnStartedV1(
            turn_index=0,
            entry_skill_id="general",
            skill_snapshot_version="v1",
            model="sim",
            budget_snapshot={"iterations": 8},
        ),
        TurnCompletedV1(
            turn_index=0,
            end_reason="complete",
            iterations=1,
            usage={"total_tokens": 3},
            final_item_ids=("item_1",),
        ),
        TurnFailedV1(turn_index=0, stable_error=error, effect_state={"safe": True}),
        TurnCancelledV1(
            turn_index=0, cancellation_reason="user", effect_state={"safe": True}
        ),
        LlmRequestCommittedV1(
            turn_index=0,
            iteration=0,
            provider="sim",
            model="sim",
            api_request={"messages": []},
            effect_kind="external",
            reconciliation="manual",
        ),
        LlmResponseCheckpointV1(
            request_record_id="request_1",
            retry_ordinal=0,
            status="complete",
            normalized_items=[{"kind": "assistant", "text": "ok"}],
        ),
        LlmResponseCommittedV1(
            request_record_id="request_1",
            checkpoint_record_id="checkpoint_1",
            status="complete",
            normalized_items=[{"kind": "assistant", "text": "ok"}],
            usage={"total_tokens": 3},
        ),
        ToolIntentCommittedV1(
            turn_index=0,
            iteration=0,
            call_id="call_1",
            name="search",
            arguments_raw='{"q":"x"}',
            effective_arguments={"q": "x"},
            parallel_safe=True,
            effect_kind="read",
            reconciliation="retry",
        ),
        ToolOutcomeCommittedV1(
            intent_record_id="intent_1",
            call_id="call_1",
            name="search",
            status="success",
            output="ok",
            data={"hits": 1},
            duration_ms=1.5,
        ),
        SkillSelectedV1(
            call_id="call_1",
            skill_id="research",
            version="v1",
            definition_hash="b" * 64,
            body_hash="c" * 64,
            full_definition={"description": "research"},
            arguments={"q": "x"},
            selection_origin="llm",
        ),
        SkillDispatchStartedV1(
            selected_record_id="selected_1",
            call_id="call_1",
            child_thread_id="thread_child",
            call_stack=("research",),
            arguments={"q": "x"},
        ),
        SkillDispatchFinishedV1(
            started_record_id="started_1",
            call_id="call_1",
            child_thread_id="thread_child",
            status="success",
        ),
        ThreadCreatedV1(
            entry_skill_id="general", source="user", tags=("audit",), extra={}
        ),
        ThreadBoundV1(session_id="session_1", thread_id="thread_1"),
        ThreadTerminalV1(status="complete", end_reason="finished"),
        SessionEndedV1(status="complete", reason="released", audit_complete=True),
    ]


def test_all_payload_dtos_are_versioned_frozen_and_extra_forbidden() -> None:
    """V1 payload 必须统一版本、深度不可变且禁止透传字段。"""
    for payload in _payload_examples():
        assert payload.payload_version == 1  # type: ignore[attr-defined]
        with pytest.raises(ValidationError):
            type(payload).model_validate(
                {**payload.model_dump(mode="python"), "unexpected": True}  # type: ignore[attr-defined]
            )

    started = _payload_examples()[3]
    with pytest.raises(TypeError, match="frozen JsonValue"):
        started.budget_snapshot["iterations"] = 9  # type: ignore[attr-defined, index]


@pytest.mark.parametrize(
    "data",
    [
        {"op_kind": "user_message", "attachments": [], "source": "cli"},
        {"op_kind": "user_message", "text": "x", "source": "cli"},
        {"op_kind": "cancel_turn"},
        {"op_kind": "cancel_turn", "target_submission_id": "sub_1", "text": "wrong"},
        {"op_kind": "cancel_turn", "target_submission_id": "sub_1", "turn_index": 1},
        {"op_kind": "shutdown", "source": "wrong"},
        {"op_kind": "shutdown", "turn_index": 1},
    ],
)
def test_submission_accepted_rejects_missing_or_cross_shape_fields(
    data: dict[str, object],
) -> None:
    """Submission discriminator 必须要求本形状字段并拒绝其他形状。"""
    with pytest.raises(ValidationError):
        SubmissionAcceptedV1.model_validate(data)


def test_submission_accepted_supports_all_three_exact_shapes() -> None:
    """UserMessage、CancelTurn 与 Shutdown 的稳定 wire 形状均可构造。"""
    assert SubmissionAcceptedV1(
        op_kind="user_message", text="x", attachments=(), source="cli"
    ).op_kind == "user_message"
    assert SubmissionAcceptedV1(
        op_kind="cancel_turn", target_submission_id="sub_1"
    ).op_kind == "cancel_turn"
    assert SubmissionAcceptedV1(op_kind="shutdown").op_kind == "shutdown"


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (LlmResponseCheckpointV1, "status", "success"),
        (ToolOutcomeCommittedV1, "status", "complete"),
        (SkillDispatchFinishedV1, "status", "complete"),
    ],
)
def test_terminal_status_enums_reject_unknown_values(
    model: type[object], field: str, value: str
) -> None:
    """LLM/Tool/Skill 状态不得静默接受契约外值。"""
    seed = next(item for item in _payload_examples() if isinstance(item, model))
    with pytest.raises(ValidationError):
        model.model_validate({**seed.model_dump(mode="python"), field: value})  # type: ignore[attr-defined]

    assert set(LlmStatus) == {"complete", "error", "cancelled", "unknown"}
    assert set(ToolStatus) == {"success", "error", "rejected", "cancelled", "unknown"}
    assert set(SkillStatus) == {"success", "error", "rejected", "cancelled", "unknown"}


def test_v0_initialization_payloads_and_ids_remain_byte_compatible() -> None:
    """Phase-1 初始化三记录不得被 V1 payload 版本污染。"""
    descriptor = SessionDescriptor(
        session_id="session_1",
        creation_operation_id="create_1",
        writer_id="writer_1",
        root_thread=RootThreadDescriptor(
            thread_id="thread_1", entry_skill_id="general"
        ),
        config={"model": "sim"},
    )
    session, thread, binding = build_initialization_records(descriptor)

    assert [item.record_id for item in (session, thread, binding)] == [
        "create_1:session_started",
        "create_1:thread_created",
        "create_1:thread_bound",
    ]
    assert all("payload_version" not in item.payload for item in (session, thread, binding))
    assert canonical_bytes(thread.payload) == (
        b'{"entry_skill_id":"general","extra":{},"source":"user","tags":[]}'
    )


def test_v1_payload_has_a_deterministic_canonical_vector() -> None:
    """DTO canonical bytes 必须固定字段顺序、数字和 Unicode 表示。"""
    payload = ToolOutcomeCommittedV1(
        intent_record_id="intent_1",
        call_id="call_1",
        name="tool",
        status="success",
        output="好",
        data={"n": 1.0},
        duration_ms=2,
    )

    assert canonical_bytes(model_canonical_data(payload)) == (
        '{"call_id":"call_1","data":{"n":1},"duration_ms":2,'
        '"intent_record_id":"intent_1","name":"tool","output":"好",'
        '"payload_version":1,"stable_error":null,"status":"success"}'
    ).encode()


def test_stable_identities_and_record_ids_are_deterministic() -> None:
    """Session 内 operation/attempt/record identity 只由稳定输入决定。"""
    ids = JournalIdentities("session_1", "thread_1", "submission_1")
    turn = ids.turn(2)
    llm = ids.llm(turn, 3)
    tool = ids.tool(turn, "call_1")

    assert turn == "thread_1:submission_1:turn:2"
    assert llm == f"{turn}:llm:3"
    assert ids.attempt(llm, 1) == f"{llm}:attempt:1"
    assert tool == f"{turn}:tool:call_1"
    assert ids.skill(tool, "research") == f"{tool}:skill:research"
    assert record_id(llm, "llm_request_committed", "attempt_1", 0) == (
        f"{llm}:llm_request_committed:attempt_1:0"
    )
    assert record_id(tool, "tool_intent_committed") == (
        f"{tool}:tool_intent_committed:none:0"
    )


def _record_factory() -> JournalRecordFactory:
    """构造具有固定 actor/session/lineage 的 record factory。"""
    return JournalRecordFactory(
        session_id="session_1",
        actor=ActorRef(kind="system", source="audit"),
        identities=JournalIdentities("session_1", "thread_1", "submission_1"),
    )


def test_record_factory_sets_identity_lineage_and_canonical_payload() -> None:
    """Factory 只写现有 lineage 字段并把 DTO 转为 canonical payload。"""
    payload = TurnStartedV1(
        turn_index=2,
        entry_skill_id="general",
        skill_snapshot_version="v1",
        model="sim",
        budget_snapshot={"nested": [1]},
    )
    turn_id = "thread_1:submission_1:turn:2"
    record = _record_factory().build(
        operation_id=turn_id,
        record_type="turn_started",
        payload=payload,
        submission_id="submission_1",
        thread_id="thread_1",
        turn_id=turn_id,
        parent_record_id="parent_1",
        causation_id="cause_1",
        correlation_id="corr_1",
    )

    assert record.schema_version == 1
    assert record.record_id == f"{turn_id}:turn_started:none:0"
    assert record.payload == model_canonical_data(payload)
    assert record.submission_id == "submission_1"
    assert record.parent_record_id == "parent_1"
    assert "turn_index" not in type(record).model_fields
    assert "call_id" not in type(record).model_fields


@pytest.mark.anyio
async def test_same_record_identity_with_changed_payload_preserves_core_conflict(
    tmp_path: Path,
) -> None:
    """Factory 不以 payload 加盐 ID，让 durable core 检测同 ID 不同内容。"""
    core = JsonlSessionJournalCore(tmp_path)
    created = await core.create_session(
        SessionDescriptor(
            session_id="session_1",
            creation_operation_id="create_1",
            writer_id="writer_1",
            root_thread=RootThreadDescriptor(
                thread_id="thread_1", entry_skill_id="general"
            ),
            config={},
        )
    )
    factory = _record_factory()
    first = factory.build(
        operation_id="op_1",
        record_type="turn_completed",
        payload=TurnCompletedV1(
            turn_index=0,
            end_reason="complete",
            iterations=1,
            usage={},
            final_item_ids=(),
        ),
    )
    changed = factory.build(
        operation_id="op_1",
        record_type="turn_completed",
        payload=TurnCompletedV1(
            turn_index=0,
            end_reason="complete",
            iterations=2,
            usage={},
            final_item_ids=(),
        ),
    )

    assert first.record_id == changed.record_id
    assert record_fingerprint(first) != record_fingerprint(changed)
    await core.append(first, lease=created.lease, expected_seq=3)
    with pytest.raises(JournalConflictError, match="content conflict"):
        await core.append(changed, lease=created.lease, expected_seq=4)


def _response_items() -> list[ResponseItem]:
    """构造 audit V1 明确支持的六种当前 item。"""
    created_at = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    payloads = {
        "user_message": {"text": "hi", "attachments": []},
        "assistant_message": {"text": "hello", "model": "sim"},
        "function_call": {"call_id": "c1", "name": "tool", "arguments": "{}"},
        "function_call_output": {"call_id": "c1", "output": "ok", "is_error": False},
        "reasoning": {"text": "think", "summary": "short"},
        "skill_outcome": {"skill_id": "research", "outcome": "success"},
    }
    return [
        ResponseItem.model_validate(
            {
                "kind": kind,
                "id": f"item_{index}",
                "thread_id": "thread_1",
                "payload": payload,
                "created_at": created_at,
                "metadata": {"index": index},
            }
        )
        for index, (kind, payload) in enumerate(payloads.items())
    ]


@pytest.mark.parametrize("item", _response_items())
def test_response_item_explicit_wire_roundtrip_preserves_all_fields(
    item: ResponseItem,
) -> None:
    """Audit 支持的六种 item 均使用显式稳定 wire contract 往返。"""
    payload = serialize_response_item(item, source_record_id="source_1")
    restored = deserialize_response_item(payload)

    assert isinstance(payload, ConversationItemV1)
    assert payload.item_version == 1
    assert payload.item_kind == item.kind
    assert payload.item_id == item.id
    assert payload.thread_id == item.thread_id
    assert payload.payload == item.payload
    assert payload.created_at == item.created_at
    assert payload.metadata == item.metadata
    assert payload.source_record_id == "source_1"
    assert restored == item


def test_unknown_response_item_is_rejected_before_serialization() -> None:
    """契约外 kind 必须在构造 wire payload/record 前失败。"""
    unknown = ResponseItem.model_construct(
        kind="compacted",
        id="item_x",
        thread_id="thread_1",
        payload={"summary": "x"},
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
        metadata={},
    )

    with pytest.raises(UnsupportedConversationItemError, match="compacted"):
        serialize_response_item(unknown, source_record_id="source_1")


@pytest.mark.parametrize(
    "item",
    [
        ResponseItem(
            kind="assistant_message",
            id="item_missing",
            thread_id="thread_1",
            payload={"text": "missing model"},
            metadata={},
        ),
        ResponseItem(
            kind="function_call",
            id="item_extra",
            thread_id="thread_1",
            payload={"call_id": "c1", "name": "tool", "arguments": "{}", "future": 1},
            metadata={},
        ),
    ],
)
def test_supported_response_item_rejects_invalid_per_kind_payload(
    item: ResponseItem,
) -> None:
    """已支持 kind 仍必须通过该 kind 的显式 required/extra 契约。"""
    with pytest.raises(ValidationError):
        serialize_response_item(item, source_record_id="source_1")


def test_skill_outcome_rejects_raw_error_detail_before_journaling() -> None:
    """Legacy skill error_detail 不得把 secret/地址原文持久化。"""
    item = ResponseItem(
        kind="skill_outcome",
        id="item_secret",
        thread_id="thread_1",
        payload={
            "skill_id": "research",
            "outcome": "failure",
            "error_detail": "secret=TOKEN at 0xDEADBEEF",
        },
        metadata={},
    )

    with pytest.raises(ValidationError):
        serialize_response_item(item, source_record_id="source_1")


def test_conversation_item_record_links_source_and_stable_identity() -> None:
    """Conversation item helper 同时固定 ordinal 身份与 source causation。"""
    item = _response_items()[0]
    record = conversation_item_record(
        _record_factory(),
        operation_id="turn_1",
        item=item,
        source_record_id="llm_response_1",
        ordinal=3,
    )

    assert record.record_id == "turn_1:conversation_item:none:3"
    assert record.record_type == "conversation_item"
    assert record.thread_id == "thread_1"
    assert record.causation_id == "llm_response_1"
    assert record.payload["source_record_id"] == "llm_response_1"


def test_records_remain_package_private_experimental_exports() -> None:
    """V1 symbols 只从 journal package 暴露，不污染 conversation 顶层。"""
    assert not hasattr(conversation, "AttachmentV1")
    assert not hasattr(conversation, "JournalRecordFactory")


def test_stable_error_never_persists_unknown_exception_text_or_address() -> None:
    """未知异常只保留稳定类型/分类/hash，绝不读取原始消息。"""
    mapped = stable_error(RuntimeError("secret=token-123 object at 0xDEADBEEF"))
    wire = canonical_bytes(model_canonical_data(mapped)).decode()

    assert mapped.code == "unknown_exception"
    assert mapped.class_name == "RuntimeError"
    assert mapped.failure_class == "unknown"
    assert mapped.safe_message is None
    assert mapped.descriptor_hash is not None
    assert "secret" not in wire
    assert "token-123" not in wire
    assert "0xDEADBEEF" not in wire
    assert "RuntimeError('" not in wire


def test_stable_error_only_allows_explicit_public_or_tool_safe_messages() -> None:
    """仅白名单 Taifeng 公开错误和 ToolResult 可提供 safe_message。"""
    public = stable_error(InvalidRequestError("public invalid request"))
    tool = stable_error(ToolResult.error("safe tool failure"))

    assert public.safe_message == "public invalid request"
    assert public.code == "invalid_request"
    assert tool.safe_message == "safe tool failure"
    assert tool.code == "tool_result_error"


def _attachment(content: bytes = b"hello") -> AttachmentV1:
    """按完整 inline base64 契约构造附件。"""
    return AttachmentV1(
        kind="file",
        media_type="text/plain",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=base64.b64encode(content).decode("ascii"),
    )


def test_attachment_validates_and_decodes_complete_inline_content() -> None:
    """合法 base64/size/SHA-256 必须无损返回原 bytes。"""
    attachment = _attachment()
    assert attachment.encoding == "base64"
    assert attachment.decoded() == b"hello"
    assert validate_attachments(
        (attachment,), max_item_bytes=5, max_total_bytes=5
    ) == (b"hello",)


@pytest.mark.parametrize(
    "attachment",
    [
        lambda: _attachment().model_copy(update={"content": "%%%"}),
        lambda: _attachment().model_copy(update={"size": 4}),
        lambda: _attachment().model_copy(update={"sha256": "0" * 64}),
    ],
)
def test_attachment_rejects_invalid_base64_size_or_digest(attachment: object) -> None:
    """附件正文的编码、长度和 digest 任一不匹配都必须拒绝。"""
    with pytest.raises(ValueError):
        attachment().decoded()  # type: ignore[operator]


def test_attachment_limits_reject_per_item_and_total_overflow() -> None:
    """大小上限由调用方注入，单项与总量均在 acceptance 前校验。"""
    one = _attachment(b"1234")
    two = _attachment(b"5678")

    with pytest.raises(ValueError, match="per-item"):
        validate_attachments((one,), max_item_bytes=3, max_total_bytes=10)
    with pytest.raises(ValueError, match="total"):
        validate_attachments((one, two), max_item_bytes=4, max_total_bytes=7)


def test_attachment_limit_rejects_large_content_before_trusting_declared_size() -> None:
    """恶意小 size 声明不得绕过解码前资源上限。"""
    disguised = _attachment(b"x" * 64).model_copy(update={"size": 1})

    with pytest.raises(ValueError, match="encoded per-item"):
        validate_attachments((disguised,), max_item_bytes=4, max_total_bytes=4)


@pytest.mark.parametrize(
    "media_type",
    ["garbage", " ", "text /plain", "./.", ".text/plain", "text/."],
)
def test_attachment_rejects_invalid_media_type(media_type: str) -> None:
    """media_type 必须是无空白的 type/subtype。"""
    with pytest.raises(ValidationError):
        AttachmentV1.model_validate(
            {**_attachment().model_dump(mode="python"), "media_type": media_type}
        )


def test_attachment_rejects_noncanonical_base64_pad_bits() -> None:
    """Zh== 虽可解码为 b'f'，但非 canonical Zg== 必须拒绝。"""
    attachment = _attachment(b"f").model_copy(update={"content": "Zh=="})

    with pytest.raises(ValueError, match="canonical base64"):
        attachment.decoded()
    with pytest.raises(ValueError, match="canonical base64"):
        validate_attachments((attachment,), max_item_bytes=1, max_total_bytes=1)


def test_attachment_rejects_reference_shapes_and_requires_content() -> None:
    """引用、临时路径和缺正文不属于 V1 attachment wire contract。"""
    data = _attachment().model_dump(mode="python")
    data.pop("content")
    with pytest.raises(ValidationError):
        AttachmentV1.model_validate(data)
    with pytest.raises(ValidationError):
        AttachmentV1.model_validate({**_attachment().model_dump(), "uri": "file:///tmp/x"})
