"""可选终端执行树；阻塞输出在线程中串行运行，不控制引擎。"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.telemetry.execution_projection import ExecutionProjection

if TYPE_CHECKING:
    from typing import TextIO

    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.event import EventMsg
    from taifeng.telemetry.execution_projection import (
        ExecutionSnapshot,
        OrchestrationPlanSnapshot,
    )


def _safe(value: str, width: int = 64) -> str:
    """过滤终端控制码与格式控制符，并限定扫描与显示长度。"""
    text = ''.join(c for c in value[: width * 4] if not unicodedata.category(c).startswith('C'))
    return text[:width] + ('…' if len(text) > width or len(value) > width * 4 else '')


def _clip_columns(text: str, columns: int) -> str:
    """按显示列裁剪，宽字符占两列、组合符零列并预留省略号。"""
    used = 0
    for index, char in enumerate(text):
        width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)
        if used + width > max(0, columns - 1):
            return text[:index] + '…'
        used += width
    return text


class TerminalTreeSink:
    """订阅完整事件流，把已观察执行显示成有限行数的调用树。"""

    def __init__(self, out: TextIO | None = None, interactive: bool | None = None,
                 max_rows: int = 40, projection: ExecutionProjection | None = None) -> None:
        """默认自动检测 TTY；非交互输出按快照追加。"""
        if max_rows <= 0:
            raise ValueError('max_rows must be positive')
        self.projection = projection if projection is not None else ExecutionProjection()
        self._out = out if out is not None else sys.stdout
        self._interactive = self._out.isatty() if interactive is None else interactive
        self._max_rows = max_rows
        self._last_rows = 0
        self._async_lock = asyncio.Lock()
        self._write_lock = threading.Lock()
        self._cancelled = threading.Event()

    def render(self) -> str:
        """无递归遍历已知节点；缺父级、环、深树均有界显示。"""
        snapshots = self.projection.snapshot()
        plans: dict[str, list[OrchestrationPlanSnapshot]] = {}
        for plan in self.projection.plan_snapshot():
            plans.setdefault(plan.execution_id, []).append(plan)
        known = {node.execution_id: node for node in snapshots}
        children: dict[str, list[ExecutionSnapshot]] = {}
        roots: list[ExecutionSnapshot] = []
        for node in snapshots:
            if node.parent_execution_id in known:
                children.setdefault(node.parent_execution_id or '', []).append(node)
            else:
                roots.append(node)
        finished = sum(node.state in ('completed', 'failed', 'cancelled') for node in snapshots)
        warning = ' | 观测不完整' if self.projection.incomplete else ''
        lines = [
            f'执行关系 | 已观察 {len(snapshots)} 节点，'
            f'计划事件 {sum(len(items) for items in plans.values())} 个，'
            f'终态 {finished}{warning}'
        ]
        visited: set[str] = set()
        stack = [(node, 0) for node in reversed(roots)]
        remaining = iter(snapshots)
        while len(visited) < len(snapshots) and len(lines) < self._max_rows:
            if not stack:
                candidate = next(remaining)
                if candidate.execution_id in visited:
                    continue
                stack.append((candidate, 0))
            node, depth = stack.pop()
            if node.execution_id in visited:
                continue
            visited.add(node.execution_id)
            annotation = ''
            if node.parent_execution_id and node.parent_execution_id not in known:
                annotation = ' [父级未知]'
            elif depth == 0 and node.parent_execution_id in known:
                annotation = ' [环/未连接]'
            lines.append(self._row(node, depth) + annotation)
            plan_events = plans.get(node.execution_id, [])
            if plan_events:
                lines.extend(self._plan_rows(
                    plan_events, depth + 1, self._max_rows - len(lines),
                ))
            stack.extend((child, depth + 1) for child in reversed(children.get(node.execution_id, [])))
        if len(visited) < len(snapshots):
            lines[-1] = f'… 显示截断，已观察节点共 {len(snapshots)}'
        return self._fit_frame(lines)

    @staticmethod
    def _plan_rows(
        plans: list[OrchestrationPlanSnapshot], depth: int, remaining: int,
    ) -> list[str]:
        """按事件顺序压缩计划轨迹，不把计划节点伪装成已执行节点。"""
        if remaining <= 0:
            return []
        prefix = '  ' * min(depth, 12) + '└─ '
        rows: list[str] = []
        for plan in plans:
            for group in plan.groups:
                if len(rows) >= remaining:
                    break
                if group.kind == 'when':
                    label = (
                        f'[计划#{plan.seq}:when] '
                        f'条件:{_safe(group.condition or "", 48)}'
                    )
                    rows.append(prefix + label)
                    continue
                for skill_id in group.skills:
                    if len(rows) >= remaining:
                        break
                    rows.append(
                        f'{prefix}[计划#{plan.seq}:{group.kind}] '
                        f'{_safe(skill_id, 40)}（待执行）'
                    )
        return rows

    def _fit_frame(self, lines: list[str]) -> str:
        """交互输出按物理终端尺寸裁剪，非交互输出保留完整字段。"""
        if self._interactive:
            columns, rows = self._terminal_size()
            limit = max(1, rows - 1)
            if len(lines) > limit:
                lines = lines[:limit]
                lines[-1] = '… 终端高度限制，显示截断'
            lines = [_clip_columns(line, max(1, columns - 1)) for line in lines]
        return '\n'.join(lines)

    def _terminal_size(self) -> os.terminal_size:
        """查询输出设备尺寸；无文件描述符时使用标准终端或保守回退。"""
        try:
            descriptor = self._out.fileno()
        except (AttributeError, OSError, ValueError):
            descriptor = 1
        try:
            return os.get_terminal_size(descriptor)
        except OSError:
            return os.terminal_size((80, 24))

    @staticmethod
    def _row(node: ExecutionSnapshot, depth: int) -> str:
        """用量、上下文与耗时分别标注，不把累计 token 当进度。"""
        end = node.finished_at or datetime.now(UTC)
        elapsed = max(0.0, (end - node.started_at).total_seconds())
        tools = ','.join(_safe(name, 24) for name in node.active_tools[:8]) or '-'
        usage = '' if node.usage_complete else '（不完整）'
        missing = f' 缺usage尝试:{node.usage_attempts_missing}' if node.usage_attempts_missing else ''
        context = '-'
        if node.context_tokens is not None:
            label = '实测' if node.context_measured else '估算'
            context = f'{node.context_tokens}/{node.context_window or "?"} {label}'
        prefix = '  ' * min(depth, 12) + ('└─ ' if depth else '')
        return (
            f'{prefix}{_safe(node.skill_id, 40) or "?"} [{_safe(node.execution_id, 32)}] '
            f'{node.state} | 工具:{tools} | {elapsed:.1f}s | '
            f'自身累计 token 入/出/总:{node.input_tokens}/{node.output_tokens}/{node.total_tokens}{usage}{missing}'
            f' | 上下文:{context} | 轮:{node.iterations} | 模型:{_safe(node.model, 32) or "?"}'
        )

    async def handle(self, ev: EventMsg) -> None:
        """投影后在线程输出；取消放弃等待且停止安排后续写入。"""
        async with self._async_lock:
            if self._cancelled.is_set():
                return
            self.projection.handle(ev)
            frame = self.render()
            rows = len(frame.splitlines())
            if self._interactive and self._last_rows:
                frame = f'\x1b[{self._last_rows}A\x1b[J' + frame
            try:
                await anyio.to_thread.run_sync(self._write, frame + '\n', abandon_on_cancel=True)
            except asyncio.CancelledError:
                self._cancelled.set()
                raise
            self._last_rows = rows

    def _write(self, frame: str) -> None:
        """线程锁保护已开始但被异步取消放弃的底层写入。"""
        with self._write_lock:
            if not self._cancelled.is_set():
                self._out.write(frame)
                if not self._cancelled.is_set():
                    self._out.flush()

    async def attach(self, engine: AgentEngine) -> None:
        """从完整信封流检测首条及后续投递缺口，再消费全局序号。"""
        expected = 0
        async for envelope in engine.subscribe_all_envelopes():
            if envelope.delivery_seq != expected:
                self.projection.incomplete = True
            expected = envelope.delivery_seq + 1
            await self.handle(envelope.event)


def attach_terminal_tree_sink(engine: AgentEngine, **kwargs: Any) -> asyncio.Task[None]:
    """创建可由宿主取消的后台订阅任务。"""
    return asyncio.create_task(TerminalTreeSink(**kwargs).attach(engine))
