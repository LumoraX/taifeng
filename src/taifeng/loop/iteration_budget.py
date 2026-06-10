"""IterationBudget —— turn 迭代预算值对象（consume / refund / 分层派生）。

参照 hermes ``agent/iteration_budget.py``（只学范式）：把裸 ``while iterations <
max_iterations`` 计数器抽成可记账对象，支持：

- ``refund``：内置工具声明「内部批量调用」语义时退还步数（不耗外层预算）；
- ``child``：``run_sub_skill`` 派生子 turn 时创建**独立实例**（与
  ``CancellationToken.child()`` 级联范式同构）。

**父子独立是有意语义**（对标 hermes）：子的消费不回写父、父子总和可超父 cap——
子 agent 的复杂度不应挤兑父预算；要全局硬顶用 K2 token 维（``max_session_tokens``）。
与 K1/K2 的「共享配额」语义刻意不同。

并发安全：单 turn 单实例、同一 asyncio task 内顺序消费——无锁（结构保证）。
"""

from __future__ import annotations


class IterationBudget:
    """迭代预算：``consume()`` 占一步（False=耗尽）、``refund(n)`` 退还（clamp 防负）。

    Args:
        cap: 预算上限（= 既有 ``max_iterations`` 语义）。
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._spent = 0

    @property
    def cap(self) -> int:
        """初始上限（``child()`` 默认继承此值，非剩余量）。"""
        return self._cap

    @property
    def spent(self) -> int:
        """净已消费步数（refund 后会回落）。"""
        return self._spent

    @property
    def remaining(self) -> int:
        """剩余步数。"""
        return self._cap - self._spent

    def consume(self) -> bool:
        """占一步。预算耗尽返回 False（不计数），否则 True。"""
        if self._spent >= self._cap:
            return False
        self._spent += 1
        return True

    def refund(self, n: int = 1) -> None:
        """退还 ``n`` 步（clamp 到已消费数，spent 永不为负）。

        仅供内核 dispatch 路径按 ``ToolSpec.refunds_iteration`` 静态声明调用；
        不暴露为 LLM 可触发语义（防模型刷预算）。
        """
        self._spent = max(0, self._spent - n)

    def child(self, cap: int | None = None) -> IterationBudget:
        """派生子预算（独立实例，不共享、不回写）。

        Args:
            cap: 子上限；默认 None = 继承父**初始** cap（非剩余——见模块 docstring
                「父子独立」语义）。
        """
        return IterationBudget(cap=self._cap if cap is None else cap)
