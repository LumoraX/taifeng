"""AgentEngine 的公开只读视图（属性、快照、估算、introspect）。

方法体从 ``engine.py`` 原样搬出（W7.1 拆文件，零行为变更）；``AgentEngine`` 类里按原名赋值，
``engine`` 仍是唯一白盒寻址面。
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from taifeng.loop.rewind import RewindCheckpoint, derive_rewind_log

if TYPE_CHECKING:
    from taifeng.context.budget import ContextBudget
    from taifeng.context.cache_stats import PromptCacheStats
    from taifeng.conversation.models import ResponseItem
    from taifeng.instructions.types import ResolvedInstruction
    from taifeng.loop.engine import AgentEngine
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.registry import SkillSnapshot


def thread_id(self: AgentEngine) -> str:
    return self._thread_id


def session_id(self: AgentEngine) -> str:
    """本 engine 所属 session 标识（恒非空；未显式传入时退回 thread_id）。

    审计可观测 层1：sink 在 attach 时捕获它，与事件 ``seq`` 复合成全局唯一
    落库主键 ``(session_id, seq)``——故 ``session_id`` 不盖在每条事件上。
    """
    return self._session_id


def register_pinned_state(self: AgentEngine, source: Any) -> None:
    """运行时注册 pinned 状态源（生效于下一次成功压缩）。

    宿主装配动作（业务持 engine 引用直调，不走 Op）。同名已注册 →
    ``ValueError``（registry 保证，禁静默覆盖）。
    """
    self._pinned_states.register(source)


def unregister_pinned_state(self: AgentEngine, name: str) -> None:
    """运行时注销 pinned 状态源；不存在 → ``KeyError``（显式失败）。"""
    self._pinned_states.unregister(name)


def entry_skill(self: AgentEngine) -> SkillDefinition:
    return self._entry_skill


def budget(self: AgentEngine) -> ContextBudget:
    """当前 ContextBudget；运行时通过 ``submit(UpdateBudget(...))`` 调整。"""
    return self._budget


def snapshot(self: AgentEngine) -> SkillSnapshot:
    return self._snapshot


def max_iterations(self: AgentEngine) -> int:
    return self._max_iterations


def max_parallel_tool_calls(self: AgentEngine) -> int:
    """单 turn 内一批 tool call 的最大并发数（构造期注入；默认 1=串行）。"""
    return self._max_parallel_tool_calls


def cache_stats(self: AgentEngine) -> PromptCacheStats:
    """跨 turn 累积的 prompt cache 统计（命中/失效/非预期破坏次数等）。

    G-CACHE：业务侧据此观测 cache 健康度；``unexpected_cache_breaks``
    高即说明有未归因的 cache 失效，需排查 provider/transport。
    """
    return self._cache_stats


def instructions_snapshot(self: AgentEngine) -> list[ResolvedInstruction]:
    """返回最近一次 resolve 的 ResolvedInstruction 列表（按 priority 升序）。

    spec Requirement (外部读取):
        - 返回 frozen dataclass 列表副本（业务侧修改不影响内部状态）。
        - engine 尚未跑过任何 turn 时，仅含 engine scope 的层。
        - 跑过 turn 后，含 engine + session + 最近一次 turn 解析结果。
    """
    if self._last_resolved:
        return list(self._last_resolved)
    # 未跑过 turn → 退回 engine scope 缓存
    return list(self._engine_scope_resolved)


def history_snapshot(self: AgentEngine) -> list[ResponseItem]:
    """返回当前 in-memory history 的快照副本（业务侧只读）。"""
    return list(self._history)


def rewind_nodes(self: AgentEngine) -> list[RewindCheckpoint]:
    """返回最近一次 root turn 的回访节点表（业务侧只读，供 UI 渲染可点节点）。

    节点含 turn_root / iteration / dispatch 三类;业务侧据 node_id 提交
    ``Rewind`` Op 回退到任一节点。turn 结束随状态回写,新 turn 会刷新本表。
    """
    return list(self._rewind_checkpoints)


async def rewind_nodes_for(self: AgentEngine, thread_id: str) -> list[RewindCheckpoint]:
    """按 thread_id 查询 rewind 节点表(thread-addressable rewind 的只读入口)。

    - 根 thread:直接返回内存表(等价 ``rewind_nodes()``,零 IO);
    - 其他 thread(典型为 detached spawn 子 thread):经 ``_load_thread_items``
      取逻辑 history(已折叠压缩区间 / 重放 rewind cut_index)→
      ``derive_rewind_log`` 派生节点表。**禁止对 raw 直接 derive**——raw 含
      被折叠/被截断的废弃项,坐标会错位(design D3)。

    Args:
        thread_id: 目标 thread;不存在的 thread 自然得到空表(load 空)。

    Returns:
        该 thread 的可寻址节点列表(turn_root / iteration / dispatch)。
    """
    if thread_id == self._thread_id:
        return list(self._rewind_checkpoints)
    return derive_rewind_log(await self._load_thread_items(thread_id))


def _session_tokens(self: AgentEngine) -> int:
    """K2 会话累计 token（共享计量器总量视图；含全部子树与续跑）。"""
    return self._usage_meter.total_tokens


def _set_session_tokens(self: AgentEngine, value: int) -> None:
    """白盒测试 / 宿主恢复用：直接设定会话累计基线（归因明细不变）。"""
    self._usage_meter.total_tokens = value


def estimate_tokens(self: AgentEngine) -> int:
    """估算当前 history 的 token 占用 —— 业务侧可据此决定是否 CompactNow。

    与 turn 内压缩判定同一口径：有实测校准锚点时走「实测 + 增量粗估」。
    """

    from taifeng.context.budget import calibrated_history_tokens, estimate_history_tokens

    return calibrated_history_tokens(
        self._history,
        self._token_calibration,
        estimate=partial(
            estimate_history_tokens,
            image_input_policy=self._image_input_policy,
            file_input_policy=self._file_input_policy,
            input_cost_estimator=self._input_cost_estimator,
            model=self._entry_skill.model or "",
        ),
    )


def usage_ratio(self: AgentEngine) -> float:
    """当前 token 用量占 context_window 的比例（0.0 ~ 1.0+）。"""
    return self.estimate_tokens() / max(self._budget.context_window, 1)


def introspect(self: AgentEngine) -> dict[str, Any]:
    """K6：/proc 式只读快照 —— 在飞 turn / spawn 配额 / 资源总量一览。

    供业务侧/运维做"ps"式观测：哪些 submission 在飞（含逐条取消态）、并发 spawn 用了多少、
    会话累计 token、事件丢弃数、cache 健康度、上下文占用。纯读、无副作用。
    """
    return {
        "thread_id": self._thread_id,
        "entry_skill_id": self._entry_skill.id,
        "running": self._running,
        # 在飞 turn（_PendingTurn 的 submission_id 列表）——保留向后兼容的纯 ID 视图
        "pending_submissions": [p.submission_id for p in self._pending.values()],
        # 在飞 turn 的逐条状态：每个在飞 turn 暴露是否已被请求取消。
        # 这是参考实现（claw-code lane_board 的存活/阻塞看板）在内核侧可纯读暴露的那一半——
        # 「卡死/超时」的 staleness 阈值判定需要墙钟+策略，按 R1 留给宿主（宿主跨两次 introspect
        # 采样 + 自有时钟即可判定）；内核只负责把"取消已请求但 turn 尚未收尾"这一事实暴露出来。
        "pending": [
            {"submission_id": p.submission_id, "cancel_requested": p.cancel.is_cancelled}
            for p in self._pending.values()
        ],
        "turn_index": self._turn_index,
        # K1 spawn 配额快照（active/total/上限）
        "spawn": self._spawn_registry.snapshot(),
        # K2 会话累计 token + 上限
        "session_tokens": self._session_tokens,
        # usage-tree-accounting：按 skill / thread 归因的会话用量明细
        "usage": self._usage_meter.snapshot(),
        "max_session_tokens": self._max_session_tokens,
        # K4 出站事件丢弃计数
        "events_dropped": self._events_dropped,
        # 上下文占用
        "context_tokens": self.estimate_tokens(),
        "context_window": self._budget.context_window,
        # G-CACHE 健康度摘要
        "cache": {
            "hits": self._cache_stats.completion_cache_hits,
            "misses": self._cache_stats.completion_cache_misses,
            "unexpected_breaks": self._cache_stats.unexpected_cache_breaks,
        },
    }
