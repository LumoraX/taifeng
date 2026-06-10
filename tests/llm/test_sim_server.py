"""sim/server.py SimServerState 单测 —— token 记账 / overflow / 前缀 cache 账本。"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import ContextOverflowError
from taifeng.llm.providers.sim.server import SimServerState
from taifeng.llm.types import ApiMessage, ApiRequest


def _req(*texts: str, system: list[str] | None = None) -> ApiRequest:
    """按交替 user/assistant 构造请求（首条 user）。"""
    roles = ["user", "assistant"]
    messages = [
        ApiMessage(role=roles[i % 2], content=t)  # type: ignore[arg-type]
        for i, t in enumerate(texts)
    ]
    return ApiRequest(model="sim-model", system_prompt=system or [], messages=messages)


def test_estimate_monotonic():
    """估算相对单调：请求变长估算必不减。"""
    short = SimServerState.estimate_tokens("abcd" * 10)
    long = SimServerState.estimate_tokens("abcd" * 20)
    assert short == 10
    assert long == 20


def test_no_window_never_overflows():
    """未声明 context_window → 不限窗。"""
    state = SimServerState()
    cache_read, cache_creation = state.admit(_req("x" * 100_000))
    assert cache_read == 0
    assert cache_creation > 0


def test_overflow_raises_and_repeats_until_actually_smaller():
    """超窗抛 ContextOverflowError；「压缩了个寂寞」（请求没真变小）→ 再次抛。"""
    state = SimServerState(context_window=50)
    big = _req("字" * 400)  # 估算 ≈ 100+ tokens
    with pytest.raises(ContextOverflowError):
        state.admit(big)
    # 压缩无效：同样大的请求再来 → 仍然拒绝
    with pytest.raises(ContextOverflowError):
        state.admit(big)
    # 真正压小后放行
    state.admit(_req("字" * 80))


def test_prefix_cache_hit_on_identical_prefix():
    """前缀一致 → cache_read > 0（resume 重建无漂移的量化断言基础）。"""
    state = SimServerState()
    first = _req("你好" * 50, system=["skill body " * 20])
    r1, _ = state.admit(first)
    assert r1 == 0  # 首次无缓存
    # 同前缀 + 追加新尾巴（多轮对话的自然形状）
    grown = _req("你好" * 50, "助手答复", system=["skill body " * 20])
    r2, c2 = state.admit(grown)
    assert r2 > 0
    assert c2 > 0  # 新尾巴是增量创建


def test_prefix_cache_drops_on_head_change():
    """head 变更（pre-turn 压缩动头）→ cache_read 如实下降为 0 或极小。"""
    state = SimServerState()
    state.admit(_req("原始开头内容" * 30))
    moved, _ = state.admit(_req("被压缩改写的开头" * 30))
    assert moved < SimServerState.estimate_tokens("原始开头内容" * 30) // 2


def test_prefix_cache_ring_buffer_eviction():
    """环形缓冲淘汰最旧：超过容量后最早的前缀不再命中。"""
    state = SimServerState()
    oldest = _req("最早的请求" * 20)
    state.admit(oldest)
    # 灌满 32 条不同前缀，挤掉 oldest
    for i in range(32):
        state.admit(_req(f"填充请求-{i}-" * 20))
    again, _ = state.admit(_req("最早的请求" * 20))
    # 与填充请求仍共享 "user\n" 这类角色前缀（~1 token），但完整前缀必不命中
    assert again <= 2


def test_reset_clears_ledger():
    state = SimServerState()
    state.admit(_req("内容" * 30))
    state.reset()
    r, _ = state.admit(_req("内容" * 30))
    assert r == 0
