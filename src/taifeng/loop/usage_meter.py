"""会话级 token 用量计量器 —— 整棵 turn 树共享一本账（usage-tree-accounting）。

参照 codex ``agent/control/budget.rs``：预算由整棵 agent 树共享记账，而非每个子 agent
各拿一份启动时的基线。差异：taifeng 单 engine 单 event loop，协作式调度下 ``add`` 是
无 await 的同步段，不需要原子类型或锁。

为什么需要：此前只有根 turn 收尾时把自身 usage 加进会话累计，``call_skill`` 子 turn 与
detached spawn 子树的 usage 从不回灌，子 runner 只拿启动瞬间的基线做 K2 检查——
兄弟子树之间互相看不见消耗，``max_session_tokens`` 可被子树整体绕过。

计量器同时按 skill / thread 归因，供 ``introspect()`` 与 ``turn_completed`` 透出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from taifeng.llm.types import TokenUsage


@dataclass
class UsageTally:
    """一个归因维度下的累计用量（可变，仅计量器内部累加）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int = 0
    samples: int = 0

    def add(self, usage: TokenUsage) -> None:
        """累加一次采样的 usage；total 缺省时按 input + output 补。"""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens or (usage.input_tokens + usage.output_tokens)
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.samples += 1

    def as_dict(self) -> dict[str, int]:
        """只读快照（JSON 友好）。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "samples": self.samples,
        }


@dataclass
class SessionUsageMeter:
    """会话级共享计量器：engine 持有一个，注入整棵 turn 树的每个 TurnRunner。

    每次采样 ``completed`` 时由所属 runner 调 ``add``——**实时**入账，而非等 turn 收尾。
    K2 会话上限检查读 ``total_tokens``，因此并发的兄弟子树彼此可见。

    Attributes:
        total_tokens: 会话累计 total_tokens（跨 turn、含全部子树）。
    """

    total_tokens: int = 0
    _by_skill: dict[str, UsageTally] = field(default_factory=dict)
    _by_thread: dict[str, UsageTally] = field(default_factory=dict)

    def add(self, usage: TokenUsage, *, thread_id: str, skill_id: str) -> None:
        """入账一次采样的 usage，并归因到 skill 与 thread。

        Args:
            usage: 单次采样的 usage（非累计值）。
            thread_id: 产生这次采样的 runner 所在 thread。
            skill_id: 产生这次采样的 entry skill id。
        """
        self.total_tokens += usage.total_tokens or (usage.input_tokens + usage.output_tokens)
        self._by_skill.setdefault(skill_id, UsageTally()).add(usage)
        self._by_thread.setdefault(thread_id, UsageTally()).add(usage)

    def thread_total(self, thread_id: str) -> int:
        """某 thread 累计 total_tokens；未出现过的 thread 为 0。"""
        tally = self._by_thread.get(thread_id)
        return tally.total_tokens if tally is not None else 0

    def snapshot(self) -> dict[str, Any]:
        """只读快照：总量 + 按 skill / thread 的归因明细。"""
        return {
            "total_tokens": self.total_tokens,
            "by_skill": {k: v.as_dict() for k, v in sorted(self._by_skill.items())},
            "by_thread": {k: v.as_dict() for k, v in sorted(self._by_thread.items())},
        }
