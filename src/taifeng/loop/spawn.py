"""SpawnSlotRegistry —— 子 skill 派发的广度准入（K1）。

参照 codex `agent/registry.rs::reserve_spawn_slot`（RAII 配额，超限→AgentLimitReached）。

内核机制（非策略）：taifeng 现有准入只限**深度**（`max_call_depth`），但**广度无界**——
一个 turn 可并发 fan-out N 个 `call_skill`、各再生 sub-TurnRunner，无并发上限，是
fork-bomb 真窟窿。本模块提供两道闸：

- ``max_concurrent``：同时存活的子 spawn 数（广度，RAII 进出）
- ``max_total``：本 registry 生命周期累计 spawn 数（runaway 兜底，单调不减）

上限值（策略）由业务在 engine 构造时注入；registry 本身只管计数（机制）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


SpawnRejectReason = Literal[
    "unknown_skill",
    "max_depth_exceeded",
    "cycle_detected",
    "not_in_whitelist",
    "cannot_call_entry_skill",
    "spawn_limit_concurrent",
    "spawn_limit_total",
]
"""子 skill 派发 / 分离发起被准入拒绝的稳定分类（spawn-reject 分类，ADR 0078）。

前五个来自 ``DispatchPolicy``（结构性门控），后两个来自 K1 配额。"""

_LIMIT_REASONS: dict[str, SpawnRejectReason] = {
    "concurrent": "spawn_limit_concurrent",
    "total": "spawn_limit_total",
}
_POLICY_REASONS: frozenset[str] = frozenset({
    "unknown_skill",
    "max_depth_exceeded",
    "cycle_detected",
    "not_in_whitelist",
    "cannot_call_entry_skill",
})


class SpawnLimitError(Exception):
    """spawn 配额超限。``kind`` ∈ {"concurrent", "total"}。"""

    def __init__(self, kind: str, limit: int) -> None:
        if kind not in _LIMIT_REASONS:
            raise ValueError(f"spawn limit kind must be concurrent / total, got {kind!r}")
        super().__init__(f"spawn_limit_exceeded: {kind} >= {limit}")
        self.kind = kind
        self.limit = limit

    @property
    def reject_reason(self) -> SpawnRejectReason:
        """配额拒绝的稳定分类。"""
        return _LIMIT_REASONS[self.kind]


class SpawnRejectedError(ValueError):
    """分离发起被结构性门控拒绝（目标不存在 / 白名单 / 深度 / 环 / entry）。

    继承 ``ValueError``：既有按 ``ValueError`` 捕获、按消息前缀匹配的调用方不受影响
    （消息仍是 ``unknown_skill: <id>`` / ``dispatch_rejected: <reason>``）。
    """

    def __init__(
        self,
        reject_reason: str,
        *,
        skill_id: str,
        path: tuple[str, ...] = (),
    ) -> None:
        """记录稳定分类、目标 skill 与裁决时的调用路径。

        Raises:
            ValueError: ``reject_reason`` 不是结构性门控的分类。
        """
        if reject_reason not in _POLICY_REASONS:
            raise ValueError(f"unknown spawn reject reason: {reject_reason!r}")
        message = (
            f"unknown_skill: {skill_id}"
            if reject_reason == "unknown_skill"
            else f"dispatch_rejected: {reject_reason}"
        )
        super().__init__(message)
        self.reject_reason: SpawnRejectReason = reject_reason  # type: ignore[assignment]
        self.skill_id = skill_id
        self.path = path


@dataclass
class SpawnSlotRegistry:
    """子 skill 派发的并发 + 累计配额（engine 持有一份，贯穿整棵 turn 树）。"""

    max_concurrent: int = 16
    max_total: int = 1000
    _active: int = 0
    _total: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @asynccontextmanager
    async def reserve(self) -> AsyncIterator[None]:
        """预留一个 spawn slot；超限抛 ``SpawnLimitError``（在 yield 前）。

        RAII：成功预留 → 退出时释放 ``_active``；``_total`` 单调不减（兜底）。
        """
        async with self._lock:
            if self._total >= self.max_total:
                raise SpawnLimitError("total", self.max_total)
            if self._active >= self.max_concurrent:
                raise SpawnLimitError("concurrent", self.max_concurrent)
            self._active += 1
            self._total += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1

    async def reserve_manual(self) -> None:
        """手动预留一个 spawn slot（超限抛 ``SpawnLimitError``）。

        ``reserve()`` 上下文管理器在退出时即释放 ``_active``，适合阻塞式 call_skill；
        但**分离式 spawn**（detached）发起方法返回后子 task 仍在跑，必须把"占用"与
        "释放"解耦——发起时 ``reserve_manual`` 占用，子 task 收尾时 ``release_manual``
        释放。``_total`` 同样单调不减（runaway 兜底）。
        """
        async with self._lock:
            if self._total >= self.max_total:
                raise SpawnLimitError("total", self.max_total)
            if self._active >= self.max_concurrent:
                raise SpawnLimitError("concurrent", self.max_concurrent)
            self._active += 1
            self._total += 1

    async def acquire_manual(
        self,
        *,
        should_abort: Callable[[], bool],
        poll_seconds: float = 0.05,
    ) -> bool:
        """等待式预留一个 spawn slot(二次驱动专用):并发满额时排队而非抛错。

        resume / rewind 重推与首发一样是一个在飞 runner,必须占 ``max_concurrent``
        (wave2b 复现 d:此前续跑不占 slot,广度闸被绕过)。但续跑不是新的发起——
        满额时抛错会把一次合法的 HITL 核销变成 error 终态,故改为轮询等待,直到
        ``should_abort()`` 为真(句柄已被 kill / 根取消)才放弃并返回 False。
        ``max_total`` 触顶仍立即抛 ``SpawnLimitError``(累计上限是 runaway 兜底,
        等待也不会回落)。成功预留返回 True,调用方 finally 必须 ``release_manual``。

        Args:
            should_abort: 每轮等待前询问是否放弃(无参谓词,纯内存检查)。
            poll_seconds: 轮询间隔(与 wait_peer 同粒度)。
        """
        while True:
            async with self._lock:
                if self._total >= self.max_total:
                    raise SpawnLimitError("total", self.max_total)
                if self._active < self.max_concurrent:
                    self._active += 1
                    self._total += 1
                    return True
            if should_abort():
                return False
            await asyncio.sleep(poll_seconds)

    def release_manual(self) -> None:
        """释放一个由 ``reserve_manual`` 占用的 slot（不取锁；``_active`` 自减）。

        与 ``reserve_manual`` 配对，供分离式 spawn 的子 task 收尾时调用。同步方法
        （收尾通常在 finally / 异常路径，避免再 await）；并发安全由 GIL + 单调语义保证。
        """
        self._active -= 1

    def snapshot(self) -> dict[str, int]:
        """best-effort 自省（不取锁）：当前并发 / 累计 / 上限。"""
        return {
            "active": self._active,
            "total": self._total,
            "max_concurrent": self.max_concurrent,
            "max_total": self.max_total,
        }
