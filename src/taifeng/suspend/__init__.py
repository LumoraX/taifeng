"""通用挂起 / resume 原语(业务无关)。

参照:openclaw 重入模型 + codex 协议形状(见
docs/superpowers/specs/2026-06-02-suspend-resume-design.md §2)。
差异:taifeng 用 function_call 无 output 的 history-gap 表示挂起点,
不重跑 tool;额外落 SuspensionRecord 标记 turn 中途断点。
"""
from __future__ import annotations

from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.resolver import ResolveError, ResolvePlan, SuspensionResolver
from taifeng.suspend.signal import SuspendSignal

__all__ = [
    "PendingRequest",
    "SuspendReason",
    "SuspensionRecord",
    "SuspendSignal",
    "SuspensionResolver",
    "ResolvePlan",
    "ResolveError",
]
