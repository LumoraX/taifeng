"""RFC 8785 canonical bytes 与 Journal hash 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from taifeng.conversation.journal.canonical import (
    canonical_bytes,
    payload_hash,
    record_fingerprint,
)
from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.conversation.journal.models import ActorRef, JournalRecord, JsonValue


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"b": 1, "a": 2}, b'{"a":2,"b":1}'),
        ({"n": 1.0}, b'{"n":1}'),
        ({"text": "€"}, '{"text":"€"}'.encode()),
    ],
)
def test_canonical_bytes_match_rfc_vectors(value: JsonValue, expected: bytes) -> None:
    """key 顺序、数字格式和 Unicode bytes 必须由 RFC 8785 唯一确定。"""
    assert canonical_bytes(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {1: "bad"},
        ("tuple",),
        object(),
    ],
)
def test_non_canonical_values_are_rejected(value: object) -> None:
    """非 JsonValue 必须在 hash/文件写入前被稳定拒绝。"""
    with pytest.raises(NonCanonicalValueError):
        canonical_bytes(value)


def _record(
    *,
    actor: ActorRef | None = None,
    occurred_at: datetime | None = None,
) -> JournalRecord:
    """构造固定 fingerprint 测试记录。"""
    return JournalRecord(
        session_id="ses_1",
        record_id="rec_1",
        operation_id="op_1",
        occurred_at=occurred_at,
        record_type="probe",
        thread_id="thr_root",
        actor=actor or ActorRef(kind="user", source="cli"),
        payload={"ok": True},
    )


def test_record_fingerprint_changes_when_actor_changes() -> None:
    """actor 属于调用方事实，变化必须改变幂等 fingerprint。"""
    first = record_fingerprint(_record(actor=ActorRef(kind="user", source="cli")))
    second = record_fingerprint(_record(actor=ActorRef(kind="user", source="api")))

    assert first != second


def test_record_fingerprint_normalizes_same_instant_to_utc() -> None:
    """相同时刻的不同 offset 必须得到同一 canonical fingerprint。"""
    utc = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
    cst = utc.astimezone(timezone(timedelta(hours=8)))

    assert record_fingerprint(_record(occurred_at=utc)) == record_fingerprint(
        _record(occurred_at=cst)
    )


def test_record_fingerprint_rejects_naive_datetime() -> None:
    """无时区 datetime 不能产生跨实现稳定 bytes。"""
    record = _record(occurred_at=datetime(2026, 7, 21, 8, 0))

    with pytest.raises(NonCanonicalValueError, match="timezone"):
        record_fingerprint(record)


def test_payload_hash_is_order_independent_sha256() -> None:
    """payload hash 是 64 位小写 SHA-256，并与 mapping 插入顺序无关。"""
    first = payload_hash({"b": [2, 3], "a": 1})
    second = payload_hash({"a": 1, "b": [2, 3]})

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
