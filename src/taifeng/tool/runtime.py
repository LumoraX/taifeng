"""ToolCallRuntime —— RwLock 并行 / 独占调度。

参照：codex codex-rs/core/src/tools/parallel.rs
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from taifeng.suspend.signal import SuspendSignal
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class _RwLock:
    """简易 read-write 锁。

    - 读锁：允许多并发（用于 parallel_safe=True 工具）
    - 写锁：独占（用于 parallel_safe=False 工具）

    无 reader 优先级倾斜——写者排队等所有当前 reader 结束；
    新 reader 在有等待写者时排队（避免 writer 饥饿）。
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writer_active = False
        self._waiting_writers = 0
        self._cond = asyncio.Condition()

    async def acquire_read(self) -> None:
        async with self._cond:
            while self._writer_active or self._waiting_writers > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self) -> None:
        async with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_write(self) -> None:
        """获取写锁（独占）。

        等待期间若被取消（CancelledError 或任何 BaseException），必须回退
        ``_waiting_writers`` 并唤醒等待者再上抛：否则幽灵写者会让此后所有
        ``acquire_read`` 永久排队（runtime 是 pool 级单例，等于全 pool 挂死）。
        ``asyncio.Condition.wait`` 在取消时会先重新拿回底层锁再抛，所以
        except 块内持锁，直接改计数是安全的。
        """
        async with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer_active or self._readers > 0:
                    await self._cond.wait()
            except BaseException:
                self._waiting_writers -= 1
                self._cond.notify_all()
                raise
            self._waiting_writers -= 1
            self._writer_active = True

    async def release_write(self) -> None:
        async with self._cond:
            self._writer_active = False
            self._cond.notify_all()


class ToolCallRuntime:
    """工具调用执行器。

    职责：
        1. 根据 ``ToolSpec.parallel_safe`` 选择读锁 / 写锁
        2. ``asyncio.wait_for`` 注入超时
        3. 取消传播（cancel.cancel() → handler 收到 CancelledError）
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._rwlock = _RwLock()

    def spec_for(self, name: str) -> ToolSpec | None:
        """按名取注册的 ToolSpec（turn 层读 refunds_iteration 等静态声明用）。"""
        return self._registry.get(name)

    async def dispatch(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        spec = self._registry.get(name)
        if spec is None:
            return ToolResult.error(f"unknown_tool: {name}", reason="not_in_registry")

        # call_skill 是"派发到子 turn"的内核 dispatcher，本身无 IO 副作用 —— 子
        # turn 内的 tool 各自走 RwLock。如果 call_skill 走写锁则与子 turn 内的
        # run_script / file_io 等独占工具形成死锁（父 call_skill 持锁等子 turn
        # 完成，子 turn 内的 run_script 等同一把写锁）。
        # 此处显式跳过 RwLock；call_skill 内部已通过 DispatchPolicy / Hook /
        # PermissionPolicy 三道串行门控保证安全。
        if name == "call_skill":
            return await self._invoke(spec, arguments, ctx)

        # 取锁
        acquire_read = spec.parallel_safe
        if acquire_read:
            await self._rwlock.acquire_read()
        else:
            await self._rwlock.acquire_write()

        try:
            return await self._invoke(spec, arguments, ctx)
        finally:
            if acquire_read:
                await self._rwlock.release_read()
            else:
                await self._rwlock.release_write()

    async def _invoke(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        try:
            ctx.cancel.raise_if_cancelled()
            return await asyncio.wait_for(
                spec.handler(arguments, ctx),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError:
            logger.warning("tool %s timed out after %ss", spec.name, spec.timeout_seconds)
            return ToolResult.error(
                f"tool_timeout: {spec.name} exceeded {spec.timeout_seconds}s",
                reason="timeout",
            )
        except asyncio.CancelledError:
            # K5 终态守卫：终结由「谁取消」决定。
            #   - token 取消（我们自己的协作取消）→ 优雅终结为 cancelled 结果，
            #     保证该 call 恰好一次终结（调用方据此配对 function_call_output）。
            #   - 非 token（外部 asyncio task.cancel）→ **不吞**、向上传播：正确
            #     asyncio 卫生，让真正的任务取消生效（否则被静默吃掉、任务无法中止）。
            if ctx.cancel.is_cancelled:
                return ToolResult.error("cancelled", reason="cancelled")
            raise
        except SuspendSignal:
            # 工具内挂起信号穿透,交由 dispatch_batch 捕获(不吞成 tool_error)
            raise
        except Exception as e:
            logger.exception("tool %s raised", spec.name)
            return ToolResult.error(f"tool_error: {e}", reason="exception", exception=str(e))
