"""run_in_background / wait_for_task —— shell 长任务后台执行 + 等待。

设计：
    - ``BackgroundTaskRegistry`` 进程内管理 task_id → 子进程映射
    - ``run_in_background`` 工具派发到 ``registry.spawn(...)``，返回 task_id
    - ``wait_for_task`` 工具派发到 ``registry.wait(...)``；超时不报错，让 LLM
      自决是否再 wait
    - 业务侧自管 registry 生命周期：构造时注入工具工厂，pool.close 前调
      ``registry.shutdown()``

不支持（spec Non-goal）：
    - stream-tail（实时拉 stdout）—— MVP 一次取完
    - 持久化到 store（仅进程内；跨进程复用需业务自管）

参照：claw-code bash.rs run_in_background；hermes-agent terminal_tool.py
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.builtins.shell import _quick_safety_check
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class BackgroundTaskTimeoutError(Exception):
    """业务侧可 catch；当前由 wait_for_task 工具吞掉转为 status=timeout。"""


@dataclass
class _BgTask:
    """单个后台任务的内部状态。"""

    task_id: str
    command: str
    proc: asyncio.subprocess.Process
    started_at: float
    max_output_bytes: int
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int | None = None
    killed: bool = False


class BackgroundTaskRegistry:
    """进程内后台任务注册表 —— 与 EnginePool 生命周期解耦。

    用法::

        registry = BackgroundTaskRegistry(max_concurrent=8)
        tools = [
            make_run_in_background_tool(registry=registry, policy=policy),
            make_wait_for_task_tool(registry=registry, default_timeout=60),
        ]
        # 业务在 pool.close 前：
        await registry.shutdown()
    """

    def __init__(self, *, max_concurrent: int = 16) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._tasks: dict[str, _BgTask] = {}
        self._lock = asyncio.Lock()
        self._max_concurrent = max_concurrent

    # ------------------------------------------------------------------
    # spawn / wait / kill / shutdown
    # ------------------------------------------------------------------

    async def spawn(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 64 * 1024,
    ) -> str:
        """启动一个 shell 子进程，返回 task_id。

        Raises:
            RuntimeError: 当前活跃 task ≥ max_concurrent
            OSError: subprocess spawn 失败
        """
        async with self._lock:
            active = sum(
                1 for t in self._tasks.values() if t.exit_code is None
            )
            if active >= self._max_concurrent:
                raise RuntimeError(
                    f"too_many_background_tasks: "
                    f"active={active} >= max={self._max_concurrent}"
                )
            task_id = f"bg_{secrets.token_hex(4)}"

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            task = _BgTask(
                task_id=task_id,
                command=command,
                proc=proc,
                started_at=time.time(),
                max_output_bytes=max_output_bytes,
            )
            self._tasks[task_id] = task

        # 启动后台收集器（锁外，避免阻塞其他 spawn）
        asyncio.create_task(self._collect(task))
        return task_id

    async def _collect(self, task: _BgTask) -> None:
        """后台收集 stdout/stderr + exit code，set _done 事件。"""
        try:
            stdout, stderr = await task.proc.communicate()
            task.stdout = stdout[: task.max_output_bytes]
            task.stderr = stderr[: task.max_output_bytes]
            task.exit_code = task.proc.returncode
        except BaseException as e:
            logger.exception("bg task %s collect failed: %s", task.task_id, e)
            task.exit_code = -1
        finally:
            task._done.set()  # noqa: SLF001

    async def wait(
        self,
        task_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """阻塞直到 task 完成或 timeout。

        Returns:
            {"status": "completed"|"timeout"|"unknown", "exit_code": int|None,
             "stdout": str, "stderr": str, "task_id": str}

        未知 task_id 走 ``status="unknown"`` 而非抛错（业务可重试）。
        Timeout 走 ``status="timeout"``（不杀进程，task 继续；可再次 wait）。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {
                "task_id": task_id,
                "status": "unknown",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            }

        try:
            if timeout is None:
                await task._done.wait()  # noqa: SLF001
            else:
                await asyncio.wait_for(
                    task._done.wait(),  # noqa: SLF001
                    timeout=timeout,
                )
        except TimeoutError:
            return {
                "task_id": task_id,
                "status": "timeout",
                "exit_code": None,
                "stdout": task.stdout.decode("utf-8", errors="replace"),
                "stderr": task.stderr.decode("utf-8", errors="replace"),
            }

        return {
            "task_id": task_id,
            "status": "completed",
            "exit_code": task.exit_code,
            "stdout": task.stdout.decode("utf-8", errors="replace"),
            "stderr": task.stderr.decode("utf-8", errors="replace"),
        }

    async def kill(self, task_id: str) -> bool:
        """杀子进程，返回是否成功找到并 kill。已 done 的 task 也返回 False。"""
        task = self._tasks.get(task_id)
        if task is None or task.exit_code is not None:
            return False
        try:
            task.proc.kill()
            task.killed = True
            await task.proc.wait()  # 等 collect 协程把 exit_code 写回
            return True
        except ProcessLookupError:
            return False

    async def shutdown(self) -> None:
        """kill 所有活跃 task + 清空 registry；幂等。"""
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for t in tasks:
            if t.exit_code is None:
                try:
                    t.proc.kill()
                    await t.proc.wait()
                except ProcessLookupError:
                    pass

    # ------------------------------------------------------------------
    # 业务侧查询辅助（非 LLM tool；spec Non-goal: 不暴露为内置 tool）
    # ------------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """快照当前所有 task 的元信息。"""
        out: list[dict[str, Any]] = []
        for t in self._tasks.values():
            out.append({
                "task_id": t.task_id,
                "command": t.command,
                "started_at": t.started_at,
                "exit_code": t.exit_code,
                "running": t.exit_code is None,
                "killed": t.killed,
            })
        return out


# ----------------------------------------------------------------------
# Tool factories
# ----------------------------------------------------------------------


def make_run_in_background_tool(
    *,
    registry: BackgroundTaskRegistry,
    policy: PermissionPolicy | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 64 * 1024,
    enable_safety_blacklist: bool = True,
) -> ToolSpec:
    """构造 run_in_background 工具。

    与 ``make_shell_exec_tool`` 的区别：本工具**不**阻塞 turn；返回 task_id
    供后续 wait_for_task 拉取结果。

    permission：与 shell_exec 同 scope（``"shell_exec"``），业务侧规则共享。
    """

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command:
            return ToolResult.error(
                "bad_args: command must be a non-empty string",
                reason="bad_args",
            )

        if enable_safety_blacklist:
            block = _quick_safety_check(command)
            if block is not None:
                return ToolResult.error(
                    f"safety_blocked: {block}", reason="safety_blocked",
                )

        if policy is not None:
            req = PermissionRequest(
                scope="shell_exec",
                target=command,
                reason="LLM 请求后台执行 shell 命令",
                metadata={
                    "thread_id": ctx.thread_id,
                    "call_id": ctx.call_id,
                    "background": True,
                },
            )
            decision = await policy.check(req)
            if not decision.granted:
                return ToolResult.error(
                    f"permission_denied: {decision.reason}",
                    reason="permission_denied",
                )

        try:
            task_id = await registry.spawn(
                command,
                cwd=cwd,
                env=env,
                max_output_bytes=max_output_bytes,
            )
        except RuntimeError as e:
            return ToolResult.error(
                f"spawn_rejected: {e}", reason="too_many_background_tasks",
            )
        except OSError as e:
            return ToolResult.error(f"spawn_error: {e}", reason="spawn_error")

        return ToolResult.ok(
            f"task spawned: {task_id}",
            task_id=task_id,
            command=command,
            started_at=time.time(),
        )

    return ToolSpec(
        name="run_in_background",
        description=(
            "Run a shell command in the background and return a task_id. "
            "Use wait_for_task to retrieve the result later. "
            "Suitable for long-running commands (build / test / fetch). "
            "Subject to the same safety blacklist as shell_exec."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute in background",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=False,
        timeout_seconds=15.0,  # spawn 本身应该很快
    )


def make_wait_for_task_tool(
    *,
    registry: BackgroundTaskRegistry,
    default_timeout: float = 120.0,
) -> ToolSpec:
    """构造 wait_for_task 工具。

    timeout / unknown task 都走 ToolResult.ok（is_error=False）—— 让 LLM
    根据 data.status 决定是否再 wait / 改策略，而不是被 error 中断思路。
    """

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return ToolResult.error(
                "bad_args: task_id must be a non-empty string",
                reason="bad_args",
            )
        timeout_raw = args.get("timeout_seconds", default_timeout)
        try:
            timeout = float(timeout_raw) if timeout_raw is not None else None
        except (TypeError, ValueError):
            return ToolResult.error(
                f"bad_args: timeout_seconds must be a number, got {timeout_raw!r}",
                reason="bad_args",
            )

        result = await registry.wait(task_id, timeout=timeout)

        # status=timeout / unknown 都走 ok（is_error=False），让 LLM 自适应
        status = result["status"]
        if status == "completed":
            body = (
                f"exit={result['exit_code']}\n"
                f"--- stdout ---\n{result['stdout']}"
            )
            if result["stderr"]:
                body += f"\n--- stderr ---\n{result['stderr']}"
            return ToolResult(
                output=body,
                is_error=result["exit_code"] != 0,
                data=result,
            )
        # timeout / unknown
        msg = (
            f"task {task_id} status={status}"
            f" (timeout was {timeout}s)" if status == "timeout" else ""
        )
        return ToolResult.ok(msg or f"task {task_id} status={status}", **result)

    return ToolSpec(
        name="wait_for_task",
        description=(
            "Wait for a background task (spawned via run_in_background) to "
            "complete or until timeout. Returns status=completed/timeout/"
            "unknown. Timeout is NOT an error — LLM can decide to wait again."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id returned by run_in_background",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        f"Wait timeout (seconds); default {default_timeout}"
                    ),
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=True,  # 只读 task 状态
        timeout_seconds=600.0,  # 外层 hard ceiling
    )
