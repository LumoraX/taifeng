"""DenialBreaker —— turn 内连续拒绝断路器（denial-thrash 防护）。

参照 codex ``guardian/mod.rs`` 的 ``GuardianRejectionCircuitBreaker``（只学范式）：
按 turn 累计 permission / hook 的 deny 结果（consecutive 连续数 + 滑窗 recent 数），
任一越过业务注入的阈值即单次触发（``interrupt_triggered`` 闩锁），turn 在迭代边界
以 ``end_reason="denial_circuit_open"`` 提前终止——替代「被拒后在 max_iterations
内空转重试白烧 token」。

设计要点（见 openspec change ``turn-resource-guards`` design D1–D3）：
- **turn 级生命周期**：每 turn 新建实例、turn 结束即弃，无跨 turn 状态（R5 ⚪）；
  不放进无状态共享的 ``PermissionPolicy``（避免并发污染）。
- **只消费 deny 结果，不改裁决路径**：计数点在 turn 的工具配对回填处统一观察
  ``ToolResult.data["reason"] ∈ {"hook_denied", "permission_denied"}``（含 HITL
  ask 超时产生的 deny —— 它就是 deny 结果）。
- **默认 None = 不启用**：行为零变化，opt-in（阈值是业务策略，R1 构造期注入）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class DenialBreakerConfig:
    """断路器阈值配置（业务注入；两阈值均 None = 永不触发）。

    Attributes:
        max_consecutive_denials: 连续 deny 触发阈值（任一成功重置计数）。
        max_recent_denials: 最近 ``window_size`` 次裁决内的 deny 数触发阈值。
        window_size: 滑窗大小。
    """

    max_consecutive_denials: int | None = None
    max_recent_denials: int | None = None
    window_size: int = 16


class DenialBreaker:
    """deny 计数器 + 单次触发闩锁。

    并发安全：单 turn 单实例、同一 asyncio task 内顺序记账——无锁（结构保证）。
    """

    def __init__(self, config: DenialBreakerConfig) -> None:
        self._config = config
        self._consecutive = 0
        # 滑窗：True=denied / False=success
        self._window: deque[bool] = deque(maxlen=max(config.window_size, 1))
        self._last_target = ""
        self._opened = False

    @property
    def opened(self) -> bool:
        """闩锁状态；置位后保持到 turn 结束（实例即弃）。"""
        return self._opened

    def record_denial(self, target: str) -> bool:
        """记一次 deny。

        Returns:
            True 当且仅当**本次**恰好越过阈值触发 open（调用方据此 emit
            ``denial_circuit_open`` 事件恰好一次）；已 open 后恒 False。
        """
        self._last_target = target
        self._consecutive += 1
        self._window.append(True)
        if self._opened:
            return False
        cfg = self._config
        recent = sum(self._window)
        hit_consecutive = (
            cfg.max_consecutive_denials is not None
            and self._consecutive >= cfg.max_consecutive_denials
        )
        hit_recent = (
            cfg.max_recent_denials is not None
            and recent >= cfg.max_recent_denials
        )
        if hit_consecutive or hit_recent:
            self._opened = True
            return True
        return False

    def record_success(self) -> None:
        """记一次成功裁决：重置 consecutive，滑窗记 False。"""
        self._consecutive = 0
        self._window.append(False)

    def snapshot(self) -> dict[str, int | str]:
        """事件 data 来源（target 仅名字，不带 args 正文 —— PII 约束）。"""
        return {
            "consecutive": self._consecutive,
            "recent": sum(self._window),
            "window_size": self._config.window_size,
            "last_denied_target": self._last_target,
        }
