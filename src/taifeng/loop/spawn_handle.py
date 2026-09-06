"""分离式 spawn 的句柄登记 + join-barrier(纯数据/机制,无 IO)。

句柄只记 child thread 引用 + 终态/结果;可由 parent thread 的 spawn 项重建(冷恢复)。
设计:ADR 0015(detached-skill-spawn);契约 docs/architecture/capabilities/detached-spawn.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SpawnStatus = Literal["running", "suspended", "done", "error", "cancelled"]
_TERMINAL: frozenset[str] = frozenset({"done", "error", "cancelled"})


@dataclass
class SpawnHandle:
    """一次分离式 spawn 的运行态句柄(handle_id ↔ child_thread_id 一一对应)。"""

    handle_id: str
    skill_id: str
    child_thread_id: str
    status: SpawnStatus = "running"
    result: str | None = None


@dataclass(frozen=True)
class SpawnDrivePlan:
    """一次 detached 子 runner 驱动的采样参数(各路径的 prepare 在线程锁内产出)。

    统一驱动入口(``SpawnDriver._drive``)只认这一个形状:首发 / 直接 resume /
    嵌套 resume / rewind 重推 / peer 唤醒各自决定「以什么 history、什么采样作用域
    起跑」,K1 / kill / 互斥 / 收敛语义则在入口内闭合(spawn-module-structure 契约)。

    Attributes:
        history: 起跑 buffer(首发 ``[seed]``;二次驱动为逻辑 history,或 rewind
            截断后的 buffer)。首项即 seed。
        sample_scope_id: Responses 逻辑采样作用域(None → runner 默认)。
        auto_retry_count: TTL 自动 retry 的谱系计数透传。
        seed_pending_call_id: rewind retry_tool 保留的悬空 call,采样前先补跑。
        cache_break_reason: 首采样 cache 失效的预期归因(rewind → "rewind")。
    """

    history: list[Any]
    sample_scope_id: str | None = None
    auto_retry_count: int = 0
    seed_pending_call_id: str | None = None
    cache_break_reason: str | None = None


@dataclass(frozen=True)
class JoinBarrier:
    """登记「{句柄集}全终态 → 起聚合 skill」;fired 幂等由 store 标记保证。"""

    barrier_id: str
    handle_ids: tuple[str, ...]
    then_skill_id: str
    then_args_template: dict[str, Any] | None = None


@dataclass
class SpawnHandleRegistry:
    """句柄登记表 + barrier 集;engine 持有,可由 store 项重建。"""

    handles: dict[str, SpawnHandle] = field(default_factory=dict)
    barriers: dict[str, JoinBarrier] = field(default_factory=dict)

    def register(self, *, handle_id: str, skill_id: str, child_thread_id: str) -> SpawnHandle:
        """注册一个新的分离式 spawn 句柄,初始状态为 running。"""
        h = SpawnHandle(handle_id=handle_id, skill_id=skill_id, child_thread_id=child_thread_id)
        self.handles[handle_id] = h
        return h

    def get(self, handle_id: str) -> SpawnHandle | None:
        """按 handle_id 查找句柄,不存在则返回 None。"""
        return self.handles.get(handle_id)

    def set_result(self, handle_id: str, *, status: SpawnStatus, result: str | None) -> None:
        """更新句柄状态与结果;handle_id 必须已注册,否则抛 KeyError。"""
        h = self.handles[handle_id]
        h.status = status
        h.result = result

    def is_terminal(self, handle_id: str) -> bool:
        """判断指定句柄是否已进入终态(done / error / cancelled)。"""
        h = self.handles.get(handle_id)
        return h is not None and h.status in _TERMINAL

    def all_terminal(self, handle_ids: list[str]) -> bool:
        """判断给定句柄列表是否全部进入终态;用于 join-barrier 触发检查。"""
        return all(self.is_terminal(hid) for hid in handle_ids)
