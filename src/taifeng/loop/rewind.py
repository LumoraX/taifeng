"""Turn 内回访节点(rewind checkpoint)侧录。

一次 root turn 的执行轨迹被拆成一张**可寻址的回访节点表**,业务侧可对任意节点
直接 retry(见 ``Rewind`` Op)。节点三类:

- ``turn_root``：整条 turn 重来(re_reason)。
- ``iteration``：每圈 LLM 采样前。rewind 它 = 重采样该圈,LLM 重决下游(re_reason)。
- ``dispatch``：每次工具 / call_skill 派发。两个切点——``history_len`` = 所属
  iteration 采样前(re_reason,与该圈 iteration 节点同值,因 assistant 消息原子、
  不可切在并行 tool_call 中间);``inner_history_len`` = function_call 之后 /
  function_call_output 之前(单调用批次的 retry_tool 切点)。

retry_tool 的实际截断由 ``plan_retry_cut`` 按**批次**规划(ADR 0079):一次采样发出多个
调用时,只去掉目标调用的旧结果,同批其他调用的调用记录与结果原样保留,批次之后的内容
丢弃。单调用批次下规划结果与 ``inner_history_len`` 相同。

设计:ADR 0014(turn-rewind)+ ADR 0016(冷场景重建);契约 docs/architecture/capabilities/turn-rewind.md
约束:checkpoint 只记 history **下标**,不物理删 store —— append-only 不破(R5)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem

RewindKind = Literal["turn_root", "iteration", "dispatch"]


def count_turns(history: list[ResponseItem]) -> int:
    """累积 user_message 数 —— 结构化 turn 序号 k 的真相来源。

    derive 与 live 记录共用此 helper，保证 node_id 的 t{k} 前缀热冷一致。
    冷加载（从 JSONL 重建）与热执行路径使用同一计数规则，不依赖
    engine._turn_index（它起始 0、冷加载不回填，会撞号）。

    Args:
        history: 当前 history buffer 中的 ResponseItem 列表。

    Returns:
        累积 user_message 数；空列表返回 0。
    """
    return sum(1 for it in history if it.kind == "user_message")


@dataclass(frozen=True)
class RewindCheckpoint:
    """turn 执行轨迹上的一个可回退锚点(只记下标,append-only 不破)。"""

    node_id: str
    turn_index: int
    """所属 root turn 序号 k(= 累积 user_message 数,1-based)。"""
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
    """root turn 的回访节点侧录;按记录序累积,node_id 全局 turn 限定唯一。"""

    checkpoints: list[RewindCheckpoint] = field(default_factory=list)
    _dispatch_seq: int = 0

    def record_iteration(
        self,
        *,
        turn_index: int,
        iteration_index: int,
        history_len: int,
        cache_anchor: int,
    ) -> RewindCheckpoint:
        """记一圈 LLM 采样前的 iteration 节点。

        node_id 格式为 t{k}:it{n}，k = turn_index（累积 user_message 数），
        n = iteration_index（本 turn 内采样圈序号）。
        """
        cp = RewindCheckpoint(
            node_id=f"t{turn_index}:it{iteration_index}",
            turn_index=turn_index,
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
        turn_index: int,
        iteration_index: int,
        iteration_history_len: int,
        cache_anchor: int,
        call_id: str,
        target_id: str,
        inner_history_len: int,
        args_digest: str,
    ) -> RewindCheckpoint:
        """记一次工具 / call_skill 派发的 dispatch 节点(两个切点)。

        node_id 格式为 t{k}:disp{m}，k = turn_index，m = 全局 dispatch 序号
        （_dispatch_seq 累计，同一 turn 内单调递增）。
        re_reason 切点归一到所属 iteration 采样前（assistant 消息原子，不可切在
        并行 tool_call 中间）。
        """
        cp = RewindCheckpoint(
            node_id=f"t{turn_index}:disp{self._dispatch_seq}",
            turn_index=turn_index,
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

    def reset_dispatch_seq(self) -> None:
        """跨 turn 重置 dispatch 序号 —— disp 编号在每个 turn 内从 0 起。

        derive_rewind_log 扫到新 turn(user_message)时调用。
        """
        self._dispatch_seq = 0

    def find(self, node_id: str) -> RewindCheckpoint | None:
        """按 node_id 查 checkpoint;不存在返回 None(调用方负责拒绝路径)。"""
        return next(
            (c for c in self.checkpoints if c.node_id == node_id), None
        )


# 属于「一次采样的工具批次」的条目类型:意图(Chat 路径写前日志)/ 调用 / 结果
_BATCH_KINDS = frozenset({"tool_intent", "function_call", "function_call_output"})


@dataclass(frozen=True)
class RetryCut:
    """retry_tool 的截断规划:保留 ``history[:cut_index]``,再去掉 ``drop_index`` 那一条。

    Attributes:
        cut_index: 截断点;其后的条目全部丢弃。
        drop_index: 保留范围内需去掉的旧结果下标;None = 无需另删(旧结果本就在截断点
            之后,或目标调用本就没有结果)。
    """

    cut_index: int
    drop_index: int | None = None

    @property
    def first_changed_index(self) -> int:
        """history 中第一个发生变化的下标(cache anchor 回退到它之前)。"""
        return self.cut_index if self.drop_index is None else self.drop_index

    def apply(self, history: list[ResponseItem]) -> list[ResponseItem]:
        """按规划产出新的 history 列表(不修改入参)。"""
        kept = list(history[: self.cut_index])
        if self.drop_index is not None:
            del kept[self.drop_index]
        return kept

    def marker_extra(self) -> dict[str, int]:
        """落进 rewind marker 的坐标(冷重建按它重放);无需另删时不带 ``drop_index``。"""
        extra = {"cut_index": self.cut_index}
        if self.drop_index is not None:
            extra["drop_index"] = self.drop_index
        return extra


def _sample_id(item: ResponseItem) -> str | None:
    """调用 / 结果所属的采样 id(Responses 路径才有);没有返回 None。"""
    key = "llm_sample_id" if item.kind == "function_call" else "origin_llm_sample_id"
    value = item.metadata.get(key)
    return value if isinstance(value, str) and value else None


def _target_call_index(history: list[ResponseItem], cp: RewindCheckpoint) -> int:
    """节点对应的那一次 function_call 的下标(call id 被复用时取节点所在位置的那次)。

    Raises:
        ValueError: history 在节点位置之前找不到该调用(节点表与 history 不一致)。
    """
    assert cp.inner_history_len is not None
    upper = min(cp.inner_history_len, len(history))
    for index in range(upper - 1, -1, -1):
        item = history[index]
        if item.kind == "function_call" and item.payload.get("call_id") == cp.call_id:
            return index
    raise ValueError(f"function_call for node {cp.node_id} not found in history")


def plan_retry_cut(history: list[ResponseItem], cp: RewindCheckpoint) -> RetryCut:
    """规划对 dispatch 节点做 retry_tool 时的截断(纯函数)。

    批次 = 目标调用所在的、由意图 / 调用 / 结果组成的连续段;遇到其他类型的条目,或属于
    另一次采样的调用 / 结果(Responses 路径下一圈可能没有文本项,靠采样 id 划界)即止。
    两种落史布局都适用:Chat 路径逐对交错(调用, 结果),Responses 路径调用成组在前、
    结果成组在后。

    Raises:
        ValueError: 节点不是 dispatch 节点,或与 history 不一致。
    """
    if cp.kind != "dispatch" or cp.call_id is None or cp.inner_history_len is None:
        raise ValueError(f"retry_tool requires a dispatch node, got {cp.kind}: {cp.node_id}")
    call_index = _target_call_index(history, cp)
    sample = _sample_id(history[call_index])
    end = call_index + 1
    output_index: int | None = None
    while end < len(history):
        item = history[end]
        if item.kind not in _BATCH_KINDS:
            break
        if item.kind != "tool_intent" and _sample_id(item) != sample:
            break
        is_output = item.kind == "function_call_output"
        if is_output and output_index is None and item.payload.get("call_id") == cp.call_id:
            output_index = end
        end += 1
    if output_index is None:
        return RetryCut(cut_index=end)
    if output_index == end - 1:
        # 旧结果是批次最后一条:截到它之前即可(单调用批次即此形态)
        return RetryCut(cut_index=output_index)
    return RetryCut(cut_index=end, drop_index=output_index)


def suspended_rewind_rejection(
    kept: list[ResponseItem],
    *,
    suspension_record_id: str,
    retried_call_id: str | None,
) -> str | None:
    """挂起态下的 rewind 能否进行(纯函数);可以返回 None,否则返回拒绝原因。

    挂起态 rewind 的语义是「不回答、回到之前重来」:截断必须把挂起 record 连同它等待的
    调用一起带走。两种情形不成立:

    - ``turn_suspended``:截断后挂起 record 仍在保留范围内(节点在挂起之后,不应出现);
    - ``sibling_calls_pending``:截断后还留着没有结果的调用,且不是本次要重跑的那一个。
      典型是对同批里已有结果的调用做 retry_tool——同批等人的调用会随挂起作废而永远
      悬空。回到采样前重来(re_reason)不留任何调用,不受此限。

    Args:
        kept: 按规划截断后的 history。
        suspension_record_id: 当前活跃挂起的 record id。
        retried_call_id: retry_tool 要重跑的调用;re_reason 为 None。
    """
    for item in kept:
        if item.kind == "suspension" and item.payload.get("record_id") == suspension_record_id:
            return "turn_suspended"
    calls = {i.payload.get("call_id") for i in kept if i.kind == "function_call"}
    outputs = {i.payload.get("call_id") for i in kept if i.kind == "function_call_output"}
    dangling = {call_id for call_id in calls - outputs if call_id is not None}
    if dangling - {retried_call_id}:
        return "sibling_calls_pending"
    return None


def derive_rewind_log(history: list[ResponseItem]) -> list[RewindCheckpoint]:
    """从逻辑 history(reconstruct 后)推导全 turn 可寻址节点表。

    输入须是与热内存等价的逻辑 history(见 reconstruct_logical_history),故下标
    与 engine 后续 _history[:cut] 截断同坐标系、自洽。纯 CPU、无副作用。

    规则(按 ItemKind 扫一遍,见 spec §5.2):
    - user_message: 进入新 turn k(=已见 user_message 数),重置 iteration/cur_iter_history_len
    - assistant_message: 记 iteration 节点(history_len=本项下标,存入游标)
    - function_call: 记 dispatch 节点(history_len=当前圈游标,inner=fc 下标+1)
    - 其余 kind(含 compacted/system_injection/spawn/...): 计入下标、不产节点(default)

    Args:
        history: reconstruct 后的逻辑 ResponseItem 列表(与热内存坐标系一致)。

    Returns:
        按记录序排列的 RewindCheckpoint 列表。
    """
    log = RewindLog()
    # 当前累积 user_message 数 = turn 序号 k(1-based)
    k = 0
    # 本 turn 内 1-based 采样圈序号
    iteration = 0
    # 当前圈采样前 history 长度(dispatch 节点的 re_reason 归一切点)
    cur_iter_history_len: int | None = None

    for idx, item in enumerate(history):
        if item.kind == "user_message":
            # 新 turn 开始:递增 turn 序号,重置圈内状态
            k += 1
            iteration = 0
            cur_iter_history_len = None
            # 跨 turn 重置 dispatch 序号(turn 内 0-based,每 turn 从 0 起)
            log.reset_dispatch_seq()

        elif item.kind == "assistant_message":
            # 每次 LLM 采样输出:进入下一圈,记 iteration 节点
            iteration += 1
            # iteration 节点的 history_len = 采样前 buffer 长度。本轮 reasoning
            # (thinking 模型,紧邻本项之前落史)是本次采样的产物,坐标须扣除,
            # 否则与热路径(采样前记录,彼时 reasoning 尚未落史)偏 1 → 热冷不一致
            sample_start = (
                idx - 1
                if idx > 0 and history[idx - 1].kind == "reasoning"
                else idx
            )
            cur_iter_history_len = sample_start
            log.record_iteration(
                turn_index=k,
                iteration_index=iteration,
                history_len=sample_start,
                cache_anchor=-1,  # 冷推导无 cache anchor 信息,用 -1 占位
            )

        elif item.kind == "function_call":
            # 工具派发:dispatch 节点;re_reason 切点归一到所属圈采样前
            # 无前导 assistant(turn 刚开始就有 fc)时退化为 fc 自身下标
            base = cur_iter_history_len if cur_iter_history_len is not None else idx
            log.record_dispatch(
                turn_index=k,
                iteration_index=max(iteration, 1),  # 防御:fc 前无 assistant 时退化到第 1 圈
                iteration_history_len=base,
                cache_anchor=-1,  # 冷推导无 cache anchor 信息
                call_id=item.payload["call_id"],
                target_id=item.payload["name"],
                # inner_history_len = fc 追加后长度(fc 下标+1),即 retry_tool 切点
                inner_history_len=idx + 1,
                args_digest=item.payload["arguments"][:200],
            )
        # 其余 kind(compacted / system_injection / spawn / suspension / ...):
        # 只占下标(idx 已累积),不产节点

    return log.checkpoints
