"""Turn 内回访节点(rewind checkpoint)侧录。

一次 root turn 的执行轨迹被拆成一张**可寻址的回访节点表**,业务侧可对任意节点
直接 retry(见 ``Rewind`` Op)。节点三类:

- ``turn_root``：整条 turn 重来(re_reason)。
- ``iteration``：每圈 LLM 采样前。rewind 它 = 重采样该圈,LLM 重决下游(re_reason)。
- ``dispatch``：每次工具 / call_skill 派发。两个切点——``history_len`` = 所属
  iteration 采样前(re_reason,与该圈 iteration 节点同值,因 assistant 消息原子、
  不可切在并行 tool_call 中间);``inner_history_len`` = function_call 之后 /
  function_call_output 之前(retry_tool 切点,只重跑该工具)。

设计:docs/superpowers/specs/2026-06-05-addressable-dispatch-rewind-design.md
约束:checkpoint 只记 history **下标**,不物理删 store —— append-only 不破(R5)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RewindKind = Literal["turn_root", "iteration", "dispatch"]


@dataclass(frozen=True)
class RewindCheckpoint:
    """turn 执行轨迹上的一个可回退锚点(只记下标,append-only 不破)。"""

    node_id: str
    kind: RewindKind
    history_len: int
    """re_reason 截断点 = 该 history 长度。"""
    cache_anchor: int
    """回退时还原的 cache_anchor_index。"""
    iteration_index: int
    """所属采样圈(dispatch 借此映射到 re_reason 截点)。"""
    # 仅 dispatch 节点:
    call_id: str | None = None
    target_id: str | None = None
    """子 skill / 工具名(供 UI / 审计)。"""
    inner_history_len: int | None = None
    """retry_tool 切点(function_call 后、function_call_output 前)。"""
    args_digest: str | None = None
    """原始 args 摘要(供 UI / 审计,非重放依赖)。"""


@dataclass
class RewindLog:
    """root turn 的回访节点侧录;按记录序累积,node_id 在本 turn 内稳定唯一。"""

    checkpoints: list[RewindCheckpoint] = field(default_factory=list)
    _dispatch_seq: int = 0

    def record_iteration(
        self, *, iteration_index: int, history_len: int, cache_anchor: int
    ) -> RewindCheckpoint:
        """记一圈 LLM 采样前的 iteration 节点。"""
        cp = RewindCheckpoint(
            node_id=f"it{iteration_index}",
            kind="iteration",
            history_len=history_len,
            cache_anchor=cache_anchor,
            iteration_index=iteration_index,
        )
        self.checkpoints.append(cp)
        return cp

    def record_dispatch(
        self,
        *,
        iteration_index: int,
        iteration_history_len: int,
        cache_anchor: int,
        call_id: str,
        target_id: str,
        inner_history_len: int,
        args_digest: str,
    ) -> RewindCheckpoint:
        """记一次工具 / call_skill 派发的 dispatch 节点(两个切点)。"""
        cp = RewindCheckpoint(
            node_id=f"disp{self._dispatch_seq}",
            kind="dispatch",
            # re_reason 切点归一到所属 iteration 采样前(assistant 消息原子)
            history_len=iteration_history_len,
            cache_anchor=cache_anchor,
            iteration_index=iteration_index,
            call_id=call_id,
            target_id=target_id,
            inner_history_len=inner_history_len,
            args_digest=args_digest,
        )
        self._dispatch_seq += 1
        self.checkpoints.append(cp)
        return cp

    def find(self, node_id: str) -> RewindCheckpoint | None:
        """按 node_id 查 checkpoint;不存在返回 None(调用方负责拒绝路径)。"""
        return next((c for c in self.checkpoints if c.node_id == node_id), None)
