"""审计 resume 人裁决 DTO 与 resolver 注入的构造期校验（ADR 0070）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.loop.audit_config import AuditConfig
from taifeng.loop.audit_resume_resolution import (
    AuditToolOutcomeResolution,
    AuditToolResolutionError,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolution_accepts_provide_and_abort() -> None:
    """provide 携带结果、abort 只需裁决人。"""
    provided = AuditToolOutcomeResolution(
        action="provide", operator_id="op", output="done", is_error=False,
    )
    aborted = AuditToolOutcomeResolution(action="abort", operator_id="op")

    assert (provided.output, aborted.output, aborted.is_error) == ("done", "", False)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"action": "retry", "operator_id": "op"}, "retry is unsupported"),
        ({"action": "abort", "operator_id": ""}, "operator_id"),
        ({"action": "provide", "operator_id": "op", "output": 1}, "output must be str"),
        ({"action": "provide", "operator_id": "op", "is_error": "yes"}, "output must be str"),
    ],
)
def test_resolution_rejects_invalid_construction(kwargs: dict[str, object], message: str) -> None:
    """不支持 retry（恢复不执行工具），裁决人必填，类型不对显式报错。"""
    with pytest.raises(ValueError, match=message):
        AuditToolOutcomeResolution(**kwargs)  # type: ignore[arg-type]


def test_resolution_error_carries_record_id() -> None:
    """裁决不适用时带出违约 record，供 resume 映射为 resolution_invalid。"""
    error = AuditToolResolutionError("rec-1", "provide cannot replace an output")

    assert error.record_id == "rec-1"
    assert "rec-1" in str(error)


def test_audit_config_rejects_non_callable_resolver(tmp_path: Path) -> None:
    """resolver 必须可调用（构造期显式报错）。"""
    with pytest.raises(ValueError, match="audit_tool_outcome_resolver_invalid"):
        AuditConfig(
            journal_core=JsonlSessionJournalCore(tmp_path / "journal"),
            writer_id="w",
            max_attachment_bytes=1,
            max_total_attachment_bytes=1,
            tool_outcome_resolver="not-callable",  # type: ignore[arg-type]
        )
