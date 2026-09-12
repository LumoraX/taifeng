"""传输失败相位分类与 URL 无泄漏测试。

覆盖 `providers/_shared.py` 的 `transport_phase_of` / `transport_error`：
按 httpx 异常**类型**判 connect / stream，且归类后的 `TransientNetworkError`
消息只含相位 + 异常类型名，绝不含请求 URL。五家 provider 共用这一对函数。
"""

from __future__ import annotations

import httpx
import pytest

from taifeng.llm.providers._shared import transport_error, transport_phase_of


def test_connect_family_maps_to_connect_phase() -> None:
    """连不上 / 连接超时 / 连接池超时 → connect 相位（此时必然零产出）。"""
    for exc in (
        httpx.ConnectError("boom"),
        httpx.ConnectTimeout("boom"),
        httpx.PoolTimeout("boom"),
    ):
        assert transport_phase_of(exc) == "connect", type(exc).__name__


def test_stream_family_maps_to_stream_phase() -> None:
    """读写超时 / 读写错误 / RemoteProtocolError（mid-stream 断连）→ stream 相位。"""
    for exc in (
        httpx.ReadTimeout("boom"),
        httpx.ReadError("boom"),
        httpx.WriteError("boom"),
        httpx.RemoteProtocolError("Server disconnected"),
    ):
        assert transport_phase_of(exc) == "stream", type(exc).__name__


def test_unknown_exception_falls_back_to_stream() -> None:
    """未知类型保守归 stream —— 不误得 connect 的「必然零产出」假设。"""
    assert transport_phase_of(RuntimeError("???")) == "stream"


def test_transport_error_strips_url() -> None:
    """消息只含相位 + 异常类型名，请求 URL / 路径都不得出现。"""
    request = httpx.Request("POST", "https://secret.example.com/v1/secret-path?k=tok")
    err = transport_error(httpx.ConnectError("failed", request=request))
    assert "secret.example.com" not in str(err)
    assert "secret-path" not in str(err)
    assert "tok" not in str(err)
    assert "ConnectError" in str(err)
    assert err.transport_phase == "connect"


@pytest.mark.parametrize("provider", ["codex", "openai", "gemini", "anthropic"])
def test_transport_error_prefixes_provider(provider: str) -> None:
    """带 provider 前缀便于在多 provider 日志里辨识来源。"""
    err = transport_error(httpx.RemoteProtocolError("cut"), provider=provider)
    assert str(err).startswith(f"{provider} transport error (stream): ")
    assert err.retryable is True
    assert err.failure_class == "provider_transport"


def test_transport_error_without_provider_has_no_prefix() -> None:
    """不传 provider 时不加前缀（openai_compat 的既有形状）。"""
    err = transport_error(httpx.ReadTimeout("slow"))
    assert str(err) == "transport error (stream): ReadTimeout"
