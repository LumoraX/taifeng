"""``ShellScriptExecutor`` —— ``language="shell"`` 的默认 subprocess 实现。

设计参见 ``docs/architecture/capabilities/script-execution.md`` §Executor / §Subprocess 隔离：

- **argv 数组**：不拼接 shell 字符串，杜绝 ``"; rm -rf /"`` 注入
- **env 白名单**：默认仅传 ``PATH / HOME / LANG``，业务 secret 不泄漏到子进程
- **stdin 关闭**：``subprocess.DEVNULL``，防止 LLM 把对话内容流入子进程
- **timeout 强制**：SIGTERM → 1s grace → SIGKILL；触发时 ``is_timeout=True / killed=True``
- **cancel 传播**：``inv.cancel`` 在 await 点触发同样的 kill 流程
- **per-stream 截断**：stdout/stderr 各自累计字节超 ``max_output_bytes`` 后停止读
- **正常结果走 ScriptResult**：``exit_code != 0`` / timeout / kill 都不抛异常

CLAUDE.md 提到 "src/ 内禁止 os.getenv 业务配置"。这里读取 ``PATH / HOME / LANG`` 是
**subprocess 系统级 env 准备**而非业务配置，ADR 0009 显式说明此豁免。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

from taifeng.skill.scripts.executor import ScriptExecutionError, ScriptExecutor
from taifeng.skill.scripts.types import ScriptInvocation, ScriptResult

# 默认 env 白名单 —— subprocess 仅可见这三个 system-level 变量
_SAFE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG")

# SIGTERM → SIGKILL 之间的宽限期
_SIGKILL_GRACE_SECONDS: float = 1.0


def _default_safe_env() -> dict[str, str]:
    """构建仅含白名单 key 的最小 env 字典。"""
    env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        # 注：此处 os.environ 是 subprocess 启动所需的 system env，不是业务配置
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # 强制 LC_ALL=C.UTF-8 以稳定 child 进程的 locale 输出
    env.setdefault("LC_ALL", "C.UTF-8")
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    return env


def _format_args_to_argv(inv: ScriptInvocation) -> list[str]:
    """按 args_schema.properties 的字段插入顺序展开 argv。

    LLM 已通过 args_schema 校验过 args；这里只做顺序展开，不再二次校验。
    """
    properties = inv.descriptor.args_schema.get("properties") or {}
    ordered: list[str] = []
    for key in properties:
        if key in inv.args:
            ordered.append(str(inv.args[key]))
    # 兜底：properties 中未声明但 args 中有的尾随追加（不打散稳定顺序）
    for key, value in inv.args.items():
        if key not in properties:
            ordered.append(str(value))
    return ordered


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
) -> tuple[str, bool]:
    """读取 stream 至多 ``max_bytes`` 字节，触发截断时返回 ``truncated=True``。"""
    if stream is None:
        return "", False
    chunks: list[bytes] = []
    received = 0
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if received + len(chunk) > max_bytes:
            remaining = max_bytes - received
            if remaining > 0:
                chunks.append(chunk[:remaining])
                received += remaining
            truncated = True
            # 截断后继续读但丢弃，防止 subprocess 因 PIPE buffer 满阻塞
            while await stream.read(4096):
                pass
            break
        chunks.append(chunk)
        received += len(chunk)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text, truncated


def _signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """对子进程所在的 process group 发信号。

    我们用 ``start_new_session=True`` 启动 subprocess，PID 即 PGID。这样
    ``/bin/sh script.sh`` 中 sh fork 出来的 grandchild（如 ``sleep``）也被一起 kill。
    """
    if proc.returncode is not None or proc.pid is None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        # 进程组已不存在 / 没权限 → 退化到只 kill 主进程
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            return


async def _terminate_with_grace(
    proc: asyncio.subprocess.Process,
    grace_seconds: float = _SIGKILL_GRACE_SECONDS,
) -> None:
    """SIGTERM 整个进程组 → 等 grace → SIGKILL。已退出的进程直接返回。"""
    if proc.returncode is not None:
        return
    _signal_process_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        _signal_process_group(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except TimeoutError:
            return


class ShellScriptExecutor:
    """``language="shell"`` 默认实现。

    Args:
        env: 覆盖默认 env 白名单；不传则使用 ``_default_safe_env()``
        extra_env: 在默认 env 之上额外合入；用于业务侧补充 ``TZ`` 等系统级 key
        shell_binary: 显式指定解释器；``None`` 表示直接 exec script（依赖 shebang
            或 ``descriptor.path`` 自身可执行）。**注意**：spec 要求 SHALL NOT 读
            shebang —— 如果业务希望走 ``/bin/sh script.sh`` 形式应显式传入
            ``shell_binary="/bin/sh"``
    """

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
        shell_binary: str | None = "/bin/sh",
    ) -> None:
        self._env_base = dict(env) if env is not None else _default_safe_env()
        if extra_env:
            self._env_base.update(extra_env)
        self._shell_binary = shell_binary

    def _build_argv(self, inv: ScriptInvocation) -> list[str]:
        script_path = inv.descriptor.path.as_posix()
        args = _format_args_to_argv(inv)
        if self._shell_binary is None:
            return [script_path, *args]
        return [self._shell_binary, script_path, *args]

    async def execute(self, inv: ScriptInvocation) -> ScriptResult:
        """spawn subprocess，等待完成或被 cancel / timeout。"""
        # cancel 在 spawn 前就触发 —— 直接转给上层 anyio 取消栈
        inv.cancel.raise_if_cancelled()

        argv = self._build_argv(inv)
        cwd = inv.descriptor.path.parent
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=self._env_base,
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return ScriptResult(
                exit_code=-2,
                stdout="",
                stderr=f"spawn_failed: {e}",
                duration_ms=int((time.monotonic() - start) * 1000),
                truncated=False,
                is_timeout=False,
                killed=False,
            )
        except OSError as e:
            raise ScriptExecutionError(
                f"spawn failed for {inv.descriptor.full_target!r}: {e}",
                descriptor=inv.descriptor,
                cause=e,
            ) from e

        # === 并发：读 stdout / stderr / 监听 cancel / 等待 process 退出 ===
        max_bytes = inv.descriptor.max_output_bytes
        stdout_task = asyncio.create_task(_drain_stream(proc.stdout, max_bytes))
        stderr_task = asyncio.create_task(_drain_stream(proc.stderr, max_bytes))
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(inv.cancel.wait_cancelled())

        is_timeout = False
        killed = False

        stdout_text = ""
        stderr_text = ""
        stdout_trunc = False
        stderr_trunc = False

        try:
            # 用 asyncio.wait 同时观测：wait_task 完成 / cancel_task 触发 / timeout 到期
            done, _pending = await asyncio.wait(
                {wait_task, cancel_task},
                timeout=inv.descriptor.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in done:
                # 正常退出 —— 不需要 kill
                pass
            elif cancel_task in done:
                killed = True
                await _terminate_with_grace(proc)
            else:
                # timeout 触发
                is_timeout = True
                killed = True
                await _terminate_with_grace(proc)
        finally:
            # 收尾：确保 wait_task 完成（kill 之后 proc.wait() 会立即返回）
            if not wait_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_task), timeout=_SIGKILL_GRACE_SECONDS + 0.5
                    )
                except (TimeoutError, asyncio.CancelledError):
                    pass
            if not cancel_task.done():
                cancel_task.cancel()
                try:
                    await cancel_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # 读流收尾：subprocess 已死 stream 应快速 EOF，给 1.5s 兜底
            for task, assign in (
                (stdout_task, "stdout"),
                (stderr_task, "stderr"),
            ):
                try:
                    text, trunc = await asyncio.wait_for(
                        asyncio.shield(task), timeout=1.5
                    )
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    text, trunc = "", False
                if assign == "stdout":
                    stdout_text, stdout_trunc = text, trunc
                else:
                    stderr_text, stderr_trunc = text, trunc

        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1

        return ScriptResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_ms=duration_ms,
            truncated=stdout_trunc or stderr_trunc,
            is_timeout=is_timeout,
            killed=killed,
        )


# === Protocol 注册检查（dev-only 静态保证） ===
def _assert_protocol(_: Any = None) -> None:
    """编译期保证 ShellScriptExecutor 实现 ScriptExecutor 协议。"""
    _x: ScriptExecutor = ShellScriptExecutor()  # noqa: F841


__all__ = ["ShellScriptExecutor"]
