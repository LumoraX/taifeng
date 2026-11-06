"""执行事件的有界纯状态投影；不读取正文，不推测未知调用关系。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from taifeng.loop.event import EventMsg
    from taifeng.loop.execution_identity import ExecutionIdentity

ExecutionState = Literal[
    'queued', 'running', 'model', 'tool', 'waiting_children', 'waiting_input',
    'completed', 'failed', 'cancelled',
]
_TERMINAL = frozenset({'completed', 'failed', 'cancelled'})
_WAIT_TOOLS = frozenset({'call_skill', 'await_skills', 'wait_peer', 'wait_any'})
_TOKEN_KEYS = ('input_tokens', 'output_tokens', 'total_tokens')


@dataclass(frozen=True)
class ExecutionSnapshot:
    """一个已观察逻辑执行的快照；token 是自身跨 attempt 累计。"""

    execution_id: str
    attempt_id: str | None
    thread_id: str
    skill_id: str
    model: str
    parent_execution_id: str | None
    parent_thread_id: str | None
    state: ExecutionState
    active_tools: tuple[str, ...]
    iterations: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    usage_complete: bool
    context_tokens: int | None
    context_window: int | None
    context_measured: bool
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class _Node:
    """每节点只保留当前 attempt 与前序用量基数，不保存 attempt 历史。"""

    __slots__ = ('view', 'tools', 'base', 'current', 'base_complete', 'current_complete')

    def __init__(self, view: ExecutionSnapshot) -> None:
        """用固定数量的槽保存内部投影状态。"""
        self.view = view
        self.tools: dict[str, str] = {}
        self.base: tuple[int, int, int] = (0, 0, 0)
        self.current: tuple[int, int, int] = (0, 0, 0)
        self.base_complete = True
        self.current_complete = False


def _count(value: object) -> int | None:
    """接受非负整数事实，拒绝布尔与伪数字。"""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class ExecutionProjection:
    """消费单 engine 全量事件，内存受节点、句柄与活动工具上限约束。"""

    def __init__(self, max_nodes: int = 2048) -> None:
        """容量必须为正；截断、迟到和序号缺口均公开标记。"""
        if max_nodes <= 0:
            raise ValueError('max_nodes must be positive')
        self.max_nodes = max_nodes
        self.incomplete = False
        self._nodes: dict[str, _Node] = {}
        self._handles: dict[str, str] = {}
        self._seq = -1

    def snapshot(self) -> tuple[ExecutionSnapshot, ...]:
        """按首次观察顺序返回不可变快照。"""
        return tuple(node.view for node in self._nodes.values())

    def handle(self, ev: EventMsg) -> None:
        """同步应用一条事实；重复不重复记账，旧事件不覆盖新状态。"""
        if ev.seq <= self._seq:
            self.incomplete |= ev.seq < self._seq
            return
        if ev.seq != self._seq + 1:
            self.incomplete = True
        self._seq = ev.seq
        if ev.msg.kind.startswith('spawn_'):
            self._spawn(ev)
            return
        identity = ev.execution
        if identity is None:
            return
        node = self._identity_node(ev, identity)
        if node is None:
            return
        if node.view.attempt_id != identity.attempt_id:
            self.incomplete = True
            return
        self._apply(node, ev)

    def _new_node(self, execution_id: str, ev: EventMsg, *, skill_id: str = '',
                  parent: ExecutionIdentity | None = None) -> _Node | None:
        """容量满时不驱逐已观察节点，防止丢失用量基数。"""
        if len(self._nodes) >= self.max_nodes:
            self.incomplete = True
            return None
        node = _Node(ExecutionSnapshot(
            execution_id=execution_id, attempt_id=None, thread_id=execution_id,
            skill_id=skill_id, model='',
            parent_execution_id=parent.execution_id if parent else None,
            parent_thread_id=parent.thread_id if parent else None,
            state='queued', active_tools=(), iterations=0, input_tokens=0,
            output_tokens=0, total_tokens=0, usage_complete=False,
            context_tokens=None, context_window=None, context_measured=False,
            started_at=ev.timestamp, updated_at=ev.timestamp, finished_at=None,
        ))
        self._nodes[execution_id] = node
        return node

    def _identity_node(self, ev: EventMsg, identity: ExecutionIdentity) -> _Node | None:
        """仅 turn_started 可切换已知 attempt，防止旧轮中间事件复活。"""
        node = self._nodes.get(identity.execution_id)
        if node is None:
            node = self._new_node(identity.execution_id, ev)
            if ev.msg.kind != 'turn_started':
                self.incomplete = True
        if node is None:
            return None
        current = node.view.attempt_id
        if current != identity.attempt_id:
            if current is not None and ev.msg.kind != 'turn_started':
                return node
            if current is not None:
                node.base = tuple(a + b for a, b in zip(node.base, node.current, strict=True))  # type: ignore[assignment]
                node.base_complete &= node.current_complete
            node.current = (0, 0, 0)
            node.current_complete = False
            node.tools.clear()
            node.view = replace(node.view, attempt_id=identity.attempt_id,
                state='running', active_tools=(), iterations=0, finished_at=None,
                context_tokens=None, context_window=None, context_measured=False,
                usage_complete=False)
        node.view = replace(node.view, thread_id=identity.thread_id,
            skill_id=identity.skill_id, model=identity.model,
            parent_execution_id=identity.parent_execution_id or node.view.parent_execution_id,
            parent_thread_id=identity.parent_thread_id or node.view.parent_thread_id)
        return node

    def _usage(self, node: _Node, data: dict[str, Any]) -> None:
        """只接受 attempt 自身绝对计数；遗漏字段保留已知下界。"""
        usage = data.get('usage')
        if isinstance(usage, dict):
            values = [_count(usage.get(key)) for key in _TOKEN_KEYS]
            node.current = tuple(max(old, new or 0) for old, new in zip(node.current, values, strict=True))  # type: ignore[assignment]
            if 'usage_complete' in data:
                node.current_complete = bool(data['usage_complete']) and all(v is not None for v in values)
        elif 'usage_complete' in data:
            node.current_complete = False
        total = tuple(a + b for a, b in zip(node.base, node.current, strict=True))
        node.view = replace(node.view, input_tokens=total[0], output_tokens=total[1],
            total_tokens=total[2], usage_complete=node.base_complete and node.current_complete)

    def _apply(self, node: _Node, ev: EventMsg) -> None:
        """按生命周期与工具事实更新阶段，工具失败本身不结束执行。"""
        data, kind = ev.msg.data, ev.msg.kind
        self._usage(node, data)
        if node.view.state in _TERMINAL:
            return
        if node.view.state == 'waiting_input' and kind != 'turn_failed':
            if kind != 'execution_updated' or data.get('phase') not in ('failed', 'cancelled'):
                return
        state = node.view.state
        if kind == 'execution_updated':
            phase = data.get('phase')
            if phase in ('model', 'running', 'failed', 'cancelled'):
                state = phase
            iteration = _count(data.get('iteration'))
            node.view = replace(node.view,
                iterations=iteration if iteration is not None else node.view.iterations,
                context_tokens=_count(data.get('context_tokens')),
                context_window=_count(data.get('context_window')),
                context_measured=bool(data.get('context_measured', False))
                    and _count(data.get('context_tokens')) is not None)
        elif kind in ('tool_call_started', 'tool_call_completed'):
            self._tool(node, kind, data)
            state = self._tool_state(node)
        elif kind == 'turn_suspended':
            state = 'waiting_input'
        elif kind == 'turn_completed':
            state = 'cancelled' if data.get('end_reason') == 'cancelled' else 'completed'
        elif kind == 'turn_failed':
            state = 'failed'
        if state in _TERMINAL or state == 'waiting_input':
            node.tools.clear()
        iterations = _count(data.get('iterations'))
        node.view = replace(node.view, state=state, active_tools=tuple(node.tools.values()),
            iterations=iterations if iterations is not None else node.view.iterations,
            updated_at=ev.timestamp, finished_at=ev.timestamp if state in _TERMINAL else None)

    def _tool(self, node: _Node, kind: str, data: dict[str, Any]) -> None:
        """调用 ID 与工具名分别保存，并约束异常流的工具数量。"""
        call_id, name = data.get('call_id'), data.get('name')
        if not isinstance(call_id, str):
            self.incomplete = True
            return
        if kind == 'tool_call_completed':
            node.tools.pop(call_id, None)
        elif isinstance(name, str):
            if len(node.tools) < self.max_nodes or call_id in node.tools:
                node.tools[call_id] = name
            else:
                self.incomplete = True

    @staticmethod
    def _tool_state(node: _Node) -> ExecutionState:
        """等待子执行优先于其他并发工具，全部完成后回到运行。"""
        if any(name in _WAIT_TOOLS for name in node.tools.values()):
            return 'waiting_children'
        return 'tool' if node.tools else 'running'

    def _spawn(self, ev: EventMsg) -> None:
        """协调事件的 execution 是发出方；句柄才能确定目标子节点。"""
        data, kind = ev.msg.data, ev.msg.kind
        handle = data.get('handle_id')
        if not isinstance(handle, str):
            return
        if kind == 'spawn_started':
            child = data.get('child_thread_id')
            if not isinstance(child, str):
                self.incomplete = True
                return
            node = self._nodes.get(child) or self._new_node(child, ev,
                skill_id=str(data.get('skill_id', '')), parent=ev.execution)
            if node is not None:
                if len(self._handles) < self.max_nodes or handle in self._handles:
                    self._handles[handle] = child
                else:
                    self.incomplete = True
            return
        child = self._handles.get(handle)
        if child is None:
            self.incomplete = True
            return
        node = self._nodes[child]
        states: dict[str, ExecutionState] = {'spawn_completed': 'completed',
            'spawn_failed': 'failed', 'spawn_cancelled': 'cancelled',
            'spawn_suspended': 'waiting_input'}
        state = states.get(kind)
        authoritative_failure = state in ('failed', 'cancelled') and node.view.state == 'completed'
        if state is not None and (node.view.state not in _TERMINAL or authoritative_failure):
            node.tools.clear()
            node.view = replace(node.view, state=state, active_tools=(),
                updated_at=ev.timestamp, finished_at=ev.timestamp if state in _TERMINAL else None)
