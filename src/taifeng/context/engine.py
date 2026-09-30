"""ContextEngine 可插拔槽位 —— 由业务决定每次采样把哪些内容发给模型（ADR 0093）。

内核默认把完整的逻辑 history 发给模型，靠压缩策略在超预算时改写 history。压缩是破坏性的：
被折叠的内容不再可取。ContextEngine 提供另一条路——**history 不动，只改发出去的视图**：

```
history（持久化的事实，source of truth）
   │  assemble：选哪些、怎么排、要不要补一条检索出来的旧内容
   ▼
view（这一次采样发给模型的条目）
```

视图可以比 history 短（只发最近几轮）、可以包含 history 里没有的条目（检索召回的片段、
业务生成的摘要），也可以与 history 完全相同（返回 None）。history 本身、落盘内容、回访节点、
压缩策略都不受影响。

内核对视图只做结构校验（工具调用与结果必须成对），不评判内容取舍。

参照 openclaw ``src/context-engine``。差异：压缩仍归 ``CompressionStrategy``（已经是可插拔的），
本槽位只管视图装配与轮后通知，不重复一个压缩入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from taifeng.context.budget import ContextBudget
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.cancellation import CancellationToken


class ContextEngineError(Exception):
    """ContextEngine 给出的视图不合法，或装配过程失败。turn 以失败告终，不退回完整 history。"""


@dataclass(frozen=True)
class AssembleRequest:
    """一次视图装配的输入。

    Attributes:
        thread_id: 所在 thread（根 thread、子 skill、分离派发的 child 各自装配）。
        entry_skill_id: 该 thread 上正在跑的 skill。
        history: 完整的逻辑 history；只读，MUST NOT 被修改。
        budget: 本 turn 生效的上下文预算。
        history_tokens: 完整 history 的 token 估算。
        cache_anchor_index: ``history`` 里已被 provider 缓存的前缀的末项下标；-1 = 没有。
        cancel: 取消 token。
    """

    thread_id: str
    entry_skill_id: str
    history: Sequence[ResponseItem]
    budget: ContextBudget
    history_tokens: int
    cache_anchor_index: int
    cancel: CancellationToken


@dataclass(frozen=True)
class AssembledContext:
    """一次视图装配的结果。

    Attributes:
        items: 这次采样发给模型的条目，按发送顺序。
        cache_invalidated: 相对上一次发出的视图，已缓存的前缀是否被破坏（R2）。
        anchor_preserved_until: ``items`` 里与上一次视图保持一致的前缀的末项下标；-1 = 没有。
            内核据此放置缓存断点。
        detail: 引擎自报的结构化计数（如 ``dropped`` / ``recalled``），随事件透出。
    """

    items: Sequence[ResponseItem]
    cache_invalidated: bool
    anchor_preserved_until: int
    detail: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnUpdate:
    """一轮结束后交给引擎的增量。

    Attributes:
        thread_id: 所在 thread。
        entry_skill_id: 该 thread 上跑的 skill。
        new_items: 本轮由 runner 产出的条目（模型回复、工具调用与结果）；触发本轮的用户消息
            在 turn 开始前已进 history，不在其中。
        history: 本轮结束时完整的逻辑 history。
    """

    thread_id: str
    entry_skill_id: str
    new_items: Sequence[ResponseItem]
    history: Sequence[ResponseItem]


@runtime_checkable
class ContextEngine(Protocol):
    """上下文引擎协议：装配每次采样的视图，并在轮后获知新增内容。"""

    name: str

    async def assemble(self, request: AssembleRequest) -> AssembledContext | None:
        """装配这一次采样的视图；返回 None = 原样发送完整 history。

        实现 MUST 可取消（R4），MUST NOT 修改 ``request.history``。视图里的工具调用与结果
        MUST 成对出现（history 里本来就悬空的调用除外）。
        """
        ...

    async def after_turn(self, update: TurnUpdate) -> None:
        """一轮结束后的通知（建索引、更新检索库）。尽力而为：抛出的异常被记录后忽略。"""
        ...


def orphan_call_ids(items: Sequence[ResponseItem]) -> set[str]:
    """工具调用与结果没有配上对的 call id（任一方缺失都算）。"""
    calls: set[str] = set()
    outputs: set[str] = set()
    for item in items:
        call_id = item.payload.get("call_id")
        if not isinstance(call_id, str):
            continue
        if item.kind == "function_call":
            calls.add(call_id)
        elif item.kind == "function_call_output":
            outputs.add(call_id)
    return calls ^ outputs


def validate_view(
    engine_name: str, history: Sequence[ResponseItem], assembled: AssembledContext,
) -> None:
    """校验视图的结构；不合法抛 ``ContextEngineError``。

    - history 非空时视图不得为空；
    - 视图里不得出现 history 里没有的悬空工具调用 / 结果；
    - ``anchor_preserved_until`` 不得越界。
    """
    items = assembled.items
    if history and not items:
        raise ContextEngineError(f"context engine {engine_name!r} returned an empty view")
    broken = orphan_call_ids(items) - orphan_call_ids(history)
    if broken:
        raise ContextEngineError(
            f"context engine {engine_name!r} returned a view with unpaired tool calls: "
            f"{sorted(broken)}"
        )
    if not -1 <= assembled.anchor_preserved_until < max(len(items), 1):
        raise ContextEngineError(
            f"context engine {engine_name!r} returned anchor_preserved_until="
            f"{assembled.anchor_preserved_until} for a view of {len(items)} item(s)"
        )


class TailWindowContextEngine:
    """参考实现：只发开头与最近几轮，history 原样保留。

    视图 = 第一条用户消息及其之前的条目（任务的起点、压缩留下的摘要）+ 最近
    ``keep_last_turns`` 轮。按用户消息切分，一轮之内的工具调用与结果不会被拆开。

    与 ``SlidingWindowStrategy`` 的区别：那个改写 history（丢掉的内容不可再取），这个只改视图。
    """

    name = "tail_window"

    def __init__(self, *, keep_last_turns: int) -> None:
        """
        Args:
            keep_last_turns: 视图保留的最近轮数（一轮从一条用户消息开始）。

        Raises:
            ValueError: 小于 1。
        """
        if keep_last_turns < 1:
            raise ValueError(f"keep_last_turns must be at least 1, got {keep_last_turns!r}")
        self._keep = keep_last_turns
        # thread → 上一次视图里「最近几轮」在 history 中的起点
        self._starts: dict[str, int] = {}

    async def assemble(self, request: AssembleRequest) -> AssembledContext | None:
        """轮数不超过上限时原样发送；否则发开头 + 最近几轮。"""
        request.cancel.raise_if_cancelled()
        history = request.history
        turns = [index for index, item in enumerate(history) if item.kind == "user_message"]
        if len(turns) <= self._keep + 1:
            self._starts.pop(request.thread_id, None)
            return None
        head_end = turns[0] + 1
        start = turns[-self._keep]
        moved = self._starts.get(request.thread_id) != start
        self._starts[request.thread_id] = start
        items = [*history[:head_end], *history[start:]]
        return AssembledContext(
            items=items,
            # 窗口起点一动，开头之后的前缀就全变了
            cache_invalidated=moved,
            anchor_preserved_until=head_end - 1 if moved else len(items) - 1,
            detail={"dropped": start - head_end, "kept_turns": self._keep},
        )

    async def after_turn(self, update: TurnUpdate) -> None:
        """无状态可更新。"""


__all__ = [
    "AssembleRequest",
    "AssembledContext",
    "ContextEngine",
    "ContextEngineError",
    "TailWindowContextEngine",
    "TurnUpdate",
    "orphan_call_ids",
    "validate_view",
]
