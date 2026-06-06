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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class SpawnLimitError(Exception):
    """spawn 配额超限。``kind`` ∈ {"concurrent", "total"}。"""

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"spawn_limit_exceeded: {kind} >= {limit}")
        self.kind = kind
        self.limit = limit


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
