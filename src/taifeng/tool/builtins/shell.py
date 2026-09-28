"""shell_exec —— 受 PermissionPolicy 约束的 shell 执行。

⚠️ 高危工具。默认 ``policy.default_mode == "ask"`` 即每次都问。
建议业务侧：
    - 加 PermissionRule 白名单（如 ``ls``, ``cat``, ``grep`` 自动允许；``rm``, ``curl`` 拒绝）
    - 用 Docker / firejail 做进程级沙盒（taifeng 不内置 OS-level sandbox）
    - 设置 ``allow_shell=False`` 让工具直接 deny

参照：claw-code crates/runtime/src/bash.rs（含 bash_validation 子模块）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from taifeng.loop.cancellation import interrupt_on_cancel
from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.command_executor import CommandExecutor, CommandSpec, LocalCommandExecutor
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from taifeng.tool.subprocess_env import default_safe_env

logger = logging.getLogger(__name__)


DEFAULT_DENY_PATTERNS = (
    "rm ",
    "rm -",
    " rm -rf",
    "sudo ",
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    ":(){",  # fork bomb
    "/dev/sd",
    "mkfs.",
    "dd if=",
    "> /dev/",
    " > /etc/",
)


def _quick_safety_check(command: str) -> str | None:
    """启发式黑名单。返回拒绝原因，None 表示通过。"""
    low = " " + command.strip().lower() + " "
    for pat in DEFAULT_DENY_PATTERNS:
        if pat in low:
            return f"blocked_pattern: {pat.strip()}"
    return None


def make_shell_exec_tool(
    *,
    policy: PermissionPolicy | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 64 * 1024,
    enable_safety_blacklist: bool = True,
    allow_shell: bool = True,
    executor: CommandExecutor | None = None,
) -> ToolSpec:
    """Shell 执行工具。

    Args:
        policy: PermissionPolicy；不提供则**每次拒绝**（强保守）
        cwd: 工作目录
        env: 环境变量。**不提供时使用最小白名单**（`tool/subprocess_env.py`），
            不继承宿主完整 os.environ —— 否则子进程可读 API key 等全部凭据。
            需要更多变量的业务显式传入完整 env。
        timeout_seconds: 单次执行超时
        max_output_bytes: 截断输出
        enable_safety_blacklist: 启用启发式黑名单
        allow_shell: 是否允许 shell expansion；False 则使用 argv 模式（更安全）
        executor: 命令执行器（sandbox-seam）；None = 本机子进程。宿主注入 Docker /
            firejail / 远端实现即可把命令隔离执行，审批 / 超时 / 取消 / 截断语义不变。
    """
    run = executor or LocalCommandExecutor()

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not command or not isinstance(command, str):
            return ToolResult.error("bad_args: command required", reason="bad_args")

        if enable_safety_blacklist:
            block = _quick_safety_check(command)
            if block is not None:
                return ToolResult.error(
                    f"safety_blocked: {block}", reason="safety_blocked",
                )

        if policy is None:
            return ToolResult.error(
                "shell_exec requires PermissionPolicy (refuse by default)",
                reason="no_policy",
            )

        req = PermissionRequest(
            scope="shell_exec",
            target=command,
            reason="LLM 请求执行 shell 命令",
            metadata={
                "thread_id": ctx.thread_id,
                "call_id": ctx.call_id,
                "cwd": cwd,
            },
        )
        decision = await policy.check(req)
        if not decision.granted:
            return ToolResult.error(
                f"permission_denied: {decision.reason}", reason="permission_denied",
            )

        # env=None → 最小白名单（不继承宿主全环境，防 API key 等凭据泄漏给子进程）
        child_env = env if env is not None else default_safe_env()
        try:
            proc = await run.start(CommandSpec(
                command=command, shell=allow_shell, cwd=cwd, env=child_env))
        except (OSError, ValueError) as e:
            # ValueError：argv 模式下 shlex 解析失败（引号不配对等）
            return ToolResult.error(f"spawn_error: {e}", reason="spawn_error")

        try:
            # R4：turn 取消 / 截止时间原地打断等待，并杀掉子进程（此前只受超时约束）
            async with interrupt_on_cancel(ctx.cancel):
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds,
                )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult.error(
                f"timeout after {timeout_seconds}s", reason="timeout",
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            if not ctx.cancel.is_cancelled:
                raise  # 外部 task 取消：照常外抛（K5）
            return ToolResult.error(
                f"cancelled ({ctx.cancel.reason})", reason="cancelled")

        out_text = stdout.decode("utf-8", errors="replace")[:max_output_bytes]
        err_text = stderr.decode("utf-8", errors="replace")[:max_output_bytes]
        body = f"exit={proc.returncode}\n--- stdout ---\n{out_text}"
        if err_text:
            body += f"\n--- stderr ---\n{err_text}"

        return ToolResult(
            output=body,
            is_error=proc.returncode != 0,
            data={
                "exit_code": proc.returncode,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            },
        )

    return ToolSpec(
        name="shell_exec",
        description=(
            "执行 shell 命令。⚠️ 高危工具，必须通过 PermissionPolicy 审批；"
            "推荐业务侧用 Docker/firejail 做进程级沙盒。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "完整 shell 命令"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=handler,
        parallel_safe=False,
        # subprocess 执行是外部不可幂等副作用，恢复需人工核对
        effect_kind="external_non_idempotent",
        reconciliation="manual",
        timeout_seconds=timeout_seconds + 5.0,
    )
