"""DoomLoopDetector —— turn 内「重复同一调用空转」检测（先警后断）。

补 turn-resource-guards 的正交盲区：``DenialBreaker`` 只认连续 **deny**、
``IterationBudget`` 只数总迭代量，二者都盖不到「模型反复用**相同参数**调**同一工具**、
每次都**成功**、却毫无进展」的 doom-loop（例：一遍遍读同一文件 / 跑同一命令）。
参照 opencode ``processor.ts`` 的重复调用检测（只学范式）。

ADR 0017 规则②（模型认知回路原语）：给模型回路补一个它自己拿不到的自我状态
——「我在原地打转」。介入采**先警后断**（escalate）：

- 连续达 ``N`` 次同签名成功调用 → ``"warn"``：注一条中性事实让模型自改（turn 续跑）；
- 警告后仍连续重复到 ``2N`` 次 → ``"open"``：断路闩锁，turn 在迭代边界提前终止
  （对齐 ``DenialBreaker`` 的迭代边界终止语义）。

设计要点（对齐 DenialBreaker）：
- **turn 级生命周期**：每 turn 新建、turn 结束即弃，无跨 turn 状态（R5 ⚪）。
- **只观察成功调用**：deny/error 由 DenialBreaker 等管，本检测器只在成功结果上记账。
- **默认 None = 不启用**：行为零变化，opt-in（阈值是业务策略，R1 构造期注入）。
- **签名 = (tool, arguments_raw)**：精确匹配 LLM 原始参数串；不做归一化（首版从简，
  确定性强、零误报）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoomLoopConfig:
    """doom-loop 阈值配置（业务注入；None = 永不触发）。

    Attributes:
        max_consecutive_repeats: 警告阈值 ``N``——连续 N 次同签名成功调用 → warn；
            断路阈值派生为 ``2N``（警告后再 N 次同签名才 open，grace 窗口 = N）。
            None = 不启用（默认）。
    """

    max_consecutive_repeats: int | None = None


class DoomLoopDetector:
    """连续同签名成功调用计数器 + 先警后断闩锁。

    并发安全：单 turn 单实例、同一 asyncio task 内顺序记账——无锁（结构保证）。
    """

    def __init__(self, config: DoomLoopConfig) -> None:
        self._cfg = config
        self._last_sig: tuple[str, str] | None = None
        self._consecutive = 0
        self._warned = False
        self._opened = False

    @property
    def opened(self) -> bool:
        """断路闩锁；置位后保持到 turn 结束（实例即弃）。"""
        return self._opened

    def record(self, tool: str, arguments_raw: str) -> str:
        """记一次**成功**工具调用，返回介入动作。

        Args:
            tool: 工具名。
            arguments_raw: LLM 原始参数串（精确匹配，不归一化）。

        Returns:
            ``"none"`` / ``"warn"``（恰好达 N 次、调用方 emit + 注提示一次）/
            ``"open"``（达 2N 次、置闩锁，调用方 emit + 迭代边界终止一次）。
            已 open 后恒 ``"none"``。
        """
        if self._opened:
            return "none"
        n = self._cfg.max_consecutive_repeats
        if n is None:
            return "none"
        sig = (tool, arguments_raw)
        if sig == self._last_sig:
            self._consecutive += 1
        else:
            # 出现不同签名 → 模型已跳出，重置连续计数与警告闩锁
            self._last_sig = sig
            self._consecutive = 1
            self._warned = False
        # 警告后仍连续重复到 2N → 断路（先判 open，使 2N 这次直接 open 而非再 warn）
        if self._warned and self._consecutive >= 2 * n:
            self._opened = True
            return "open"
        # 首次达 N → 警告一次
        if not self._warned and self._consecutive >= n:
            self._warned = True
            return "warn"
        return "none"

    def snapshot(self) -> dict[str, int | str]:
        """事件 data 来源——只带工具名 + 计数 + 阈值，**不带 args 正文**（PII 约束）。"""
        return {
            "tool": self._last_sig[0] if self._last_sig else "",
            "consecutive": self._consecutive,
            "threshold": self._cfg.max_consecutive_repeats or 0,
        }
