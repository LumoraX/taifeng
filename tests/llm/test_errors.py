"""传输失败分类（transport_phase）与 classify_failure 兼容性测试。

覆盖 `TransientNetworkError` 携带
`transport_phase`（connect / stream）判别位，默认 `stream`（保守：按“已开始”对待，
只吃常规流预算而非宽松连接预算），且不破坏既有 `classify_failure` 归类。
"""

from __future__ import annotations

from taifeng.llm.errors import (
    TransientNetworkError,
    classify_failure,
)


def test_transient_network_default_phase_is_stream() -> None:
    """未显式声明 phase 时默认 stream（保守，不误得宽松 connect 预算）。"""
    err = TransientNetworkError("boom")
    assert err.transport_phase == "stream"
    # 既有契约不变
    assert err.retryable is True
    assert err.kind == "transient_network"
    assert err.failure_class == "provider_transport"


def test_transient_network_connect_phase() -> None:
    """显式 connect：首 token 前连接建立失败。"""
    err = TransientNetworkError("cannot connect", transport_phase="connect")
    assert err.transport_phase == "connect"
    assert err.failure_class == "provider_transport"


def test_transient_network_stream_phase() -> None:
    """显式 stream：mid-stream 断流。"""
    err = TransientNetworkError("stream broke", transport_phase="stream")
    assert err.transport_phase == "stream"


def test_classify_failure_still_maps_transport_regardless_of_phase() -> None:
    """两类 phase 均归 provider_transport，不新增 FailureClass 桶。"""
    for phase in ("connect", "stream"):
        cls, action = classify_failure(
            TransientNetworkError("x", transport_phase=phase)  # type: ignore[arg-type]
        )
        assert cls == "provider_transport"
        assert isinstance(action, str) and action
