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
import shlex
from typing import Any

from taifeng.permission.types import PermissionPolicy, PermissionRequest
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

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
) -> ToolSpec:
    """Shell 执行工具。

    Args:
        policy: PermissionPolicy；不提供则**每次拒绝**（强保守）
        cwd: 工作目录
        env: 环境变量（不提供则继承）
        timeout_seconds: 单次执行超时
        max_output_bytes: 截断输出
        enable_safety_blacklist: 启用启发式黑名单
        allow_shell: 是否允许 shell expansion；False 则使用 argv 模式（更安全）
    """

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

        try:
            if allow_shell:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            else:
                argv = shlex.split(command)
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
        except OSError as e:
            return ToolResult.error(f"spawn_error: {e}", reason="spawn_error")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult.error(
                f"timeout after {timeout_seconds}s", reason="timeout",
            )

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
