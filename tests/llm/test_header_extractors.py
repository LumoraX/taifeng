"""G3：响应头解析 —— request-id 兜底链 + 结构化 rate-limit 窗口。"""

from __future__ import annotations

from taifeng.llm.providers._shared import (
    _parse_reset_duration,
    extract_rate_limit_snapshot,
    extract_request_id,
)


def test_request_id_prefers_x_request_id() -> None:
    headers = {"x-request-id": "req-1", "cf-ray": "ray-1"}
    assert extract_request_id(headers) == "req-1"


def test_request_id_fallback_chain() -> None:
    assert extract_request_id({"cf-ray": "ray-9"}) == "ray-9"
    assert extract_request_id({"anthropic-request-id": "ant-3"}) == "ant-3"
    assert extract_request_id({"x-amzn-requestid": "amz-7"}) == "amz-7"


def test_request_id_absent_returns_none() -> None:
    assert extract_request_id({"content-type": "application/json"}) is None


def test_parse_reset_duration_forms() -> None:
    assert _parse_reset_duration("2") == 2.0          # 纯数字（秒）
    assert _parse_reset_duration("1s") == 1.0
    assert _parse_reset_duration("100ms") == 0.1
    assert _parse_reset_duration("6m0s") == 360.0
    assert _parse_reset_duration("1h") == 3600.0
    assert _parse_reset_duration(None) is None
    assert _parse_reset_duration("garbage") is None


def test_rate_limit_snapshot_openai_family() -> None:
    headers = {
        "x-ratelimit-remaining-requests": "59",
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-remaining-tokens": "12000",
        "x-ratelimit-reset-tokens": "6m0s",
    }
    snap = extract_rate_limit_snapshot(headers)
    assert snap is not None
    assert snap.requests_remaining == 59
    assert snap.requests_reset_seconds == 1.0
    assert snap.tokens_remaining == 12000
    assert snap.tokens_reset_seconds == 360.0
    assert snap.raw  # 原始头全留


def test_rate_limit_snapshot_anthropic_variants() -> None:
    headers = {
        "anthropic-ratelimit-requests-remaining": "10",
        "anthropic-ratelimit-tokens-remaining": "5000",
    }
    snap = extract_rate_limit_snapshot(headers)
    assert snap is not None
    assert snap.requests_remaining == 10
    assert snap.tokens_remaining == 5000


def test_rate_limit_snapshot_absent_returns_none() -> None:
    assert extract_rate_limit_snapshot({"content-type": "application/json"}) is None
