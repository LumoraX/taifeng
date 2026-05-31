"""``run_script`` 内置工具 —— SKILL.md 中声明的 script 的统一执行入口。

参见 ``docs/architecture/capabilities/script-execution.md`` §run_script
和 ``docs/architecture/capabilities/script-execution.md``。

ToolContext.extras 必须含（由 TurnRunner 注入）：
    - ``skill_snapshot``: SkillSnapshot
    - ``script_executors``: ``dict[ScriptLanguage, ScriptExecutor]``
    - ``current_skill``: SkillDefinition（caller）

ToolContext.extras 可选：
    - ``permission_policy``: PermissionPolicy | None
    - ``hook_runner``: HookRunner | None
    - ``submission_id`` / ``entry_skill_id`` / ``turn_index``
    - ``request_metadata``: dict —— 业务侧不透明上下文，合并进 PermissionRequest.metadata

9 阶段流程（spec ``script-execution``）：
    1. skill 查找 → ``unknown_skill``
    2. script_name 查找 → ``unknown_script``
    3. args_schema 校验 → ``invalid_args``
    4. executor 查找 → ``no_executor_for_language``
    5. pre_script_use hook → deny / args_override
    6. PermissionPolicy.check(scope='script_exec') → deny
    7. executor.execute(invocation) → ScriptResult
    8. post_script_use hook（仅审计；不影响 ToolResult）
    9. 打包 ToolResult（成功 ok / 失败 error，含 stderr/exit_code/is_timeout/killed）
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from taifeng.hooks.types import (
    SCRIPT_OUTPUT_PREVIEW_LIMIT,
    HookContext,
    PostScriptUseHook,
    PreScriptUseHook,
)
from taifeng.permission.types import PermissionRequest
from taifeng.skill.scripts.executor import ScriptExecutionError
from taifeng.skill.scripts.types import ScriptDescriptor, ScriptInvocation, ScriptResult
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.skill.scripts.executor import ScriptExecutor

logger = logging.getLogger(__name__)


def _truncate_preview(text: str, limit: int = SCRIPT_OUTPUT_PREVIEW_LIMIT) -> str:
    """按字节截断到 ``limit``，并丢弃尾部不完整 UTF-8 字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _validate_args(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """轻量 args_schema 校验 —— 仅检查 required 字段存在。

    完整 JSON Schema 校验留给业务侧 hook（避免 src 引入 jsonschema 依赖）。
    返回错误描述或 ``None``。
    """
    if schema.get("type") and schema["type"] != "object":
        return f"args_schema.type must be 'object', got {schema['type']!r}"
    required = schema.get("required") or []
    if not isinstance(required, list):
        return "args_schema.required must be list"
    missing = [k for k in required if k not in args]
    if missing:
        return f"missing required args: {missing}"
    return None


async def _emit_event(ctx: ToolContext, kind: str, data: dict[str, Any]) -> None:
    """通过 dispatcher emit telemetry event；找不到 dispatcher 时静默 noop。

    Script lifecycle event 复用 ``EngineLog``（其 data 中含 event kind 区分），
    避免在 EventMsg union 中为 5 个 script event 各加一个 dataclass。
    """
    dispatcher = ctx.extras.get("dispatcher")
    if dispatcher is None:
        return
    # 延迟 import 防止循环依赖
    from taifeng.loop.event import EngineLog

    # 注：TurnRunner._emit 会包一层 EventMsg；这里只传内层 msg 即可
    msg = EngineLog(data={"event": kind, **data})
    emit = getattr(dispatcher, "_emit", None)
    if emit is None:
        return
    try:
        await emit(msg)
    except Exception:
        logger.exception("emit %s failed", kind)


def _find_descriptor(
    skill_scripts: tuple[ScriptDescriptor, ...],
    script_name: str,
) -> ScriptDescriptor | None:
    for s in skill_scripts:
        if s.name == script_name:
            return s
    return None


def _build_post_hook(
    descriptor: ScriptDescriptor,
    result: ScriptResult,
) -> PostScriptUseHook:
    return PostScriptUseHook(
        skill_id=descriptor.skill_id,
        script_name=descriptor.name,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        is_timeout=result.is_timeout,
        killed=result.killed,
        truncated=result.truncated,
        stdout_preview=_truncate_preview(result.stdout),
        stderr_preview=_truncate_preview(result.stderr),
    )


async def _run_script_handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """run_script handler —— 9 阶段流程。"""
    # ---- 参数 ----
    skill_id = args.get("skill_id")
    script_name = args.get("script_name")
    script_args = args.get("args") or {}
    if not skill_id or not isinstance(skill_id, str):
        return ToolResult.error(
            "missing_or_invalid_argument: skill_id", reason="bad_args"
        )
    if not script_name or not isinstance(script_name, str):
        return ToolResult.error(
            "missing_or_invalid_argument: script_name", reason="bad_args"
        )
    if not isinstance(script_args, dict):
        return ToolResult.error(
            "invalid_argument: args must be object", reason="bad_args"
        )

    # ---- 上下文依赖 ----
    snapshot = ctx.extras.get("skill_snapshot")
    executors: dict[str, ScriptExecutor] | None = ctx.extras.get("script_executors")
    if snapshot is None or executors is None:
        return ToolResult.error(
            "run_script misconfigured: missing skill_snapshot / script_executors",
            reason="config_error",
        )

    # ---- 阶段 1：skill 查找 ----
    skill = snapshot.get(skill_id)
    if skill is None:
        return ToolResult.error(f"unknown_skill: {skill_id}", reason="unknown_skill")

    # ---- 阶段 2：script_name 查找 ----
    descriptor = _find_descriptor(skill.scripts, script_name)
    if descriptor is None:
        return ToolResult.error(
            f"unknown_script: {skill_id}/{script_name}",
            reason="unknown_script",
        )

    # ---- 阶段 3：args 校验 ----
    err = _validate_args(script_args, descriptor.args_schema)
    if err is not None:
        return ToolResult.error(f"invalid_args: {err}", reason="invalid_args")

    # ---- 阶段 4：executor 查找 ----
    executor = executors.get(descriptor.language)
    if executor is None:
        return ToolResult.error(
            f"no_executor_for_language: {descriptor.language}",
            reason="no_executor_for_language",
        )

    # ---- 可选依赖 ----
    permission_policy = ctx.extras.get("permission_policy")
    hook_runner = ctx.extras.get("hook_runner")
    request_metadata = ctx.extras.get("request_metadata") or {}
    submission_id = ctx.extras.get("submission_id") or ""
    entry_skill_id = ctx.extras.get("entry_skill_id") or ""
    turn_index = int(ctx.extras.get("turn_index") or 0)
    call_stack = ctx.extras.get("call_stack")
    call_chain: tuple[str, ...] = (
        tuple(call_stack.path()) if call_stack is not None else (skill_id,)
    )

    hook_ctx = HookContext(
        thread_id=ctx.thread_id,
        submission_id=submission_id,
        entry_skill_id=entry_skill_id,
        extras={
            "request_metadata": request_metadata,
            "call_chain": list(call_chain),
            "turn_index": turn_index,
        },
    )

    # ---- 阶段 5：pre_script_use hook ----
    # 注：直接遍历 registry，不走 HookRunner.run —— 后者在全部 allow 时返回
    # HookDecision.ok() 会丢失 metadata['args_override']。
    if hook_runner is not None:
        pre_hook = PreScriptUseHook(
            skill_id=skill_id,
            script_name=script_name,
            language=descriptor.language,
            args=dict(script_args),
            timeout_seconds=descriptor.timeout_seconds,
        )
        for handler in hook_runner.registry.handlers("pre_script_use"):
            try:
                decision = await handler(pre_hook, hook_ctx)
            except Exception as e:  # noqa: BLE001
                logger.exception("pre_script_use hook raised")
                return ToolResult.error(
                    f"hook_error: {e}",
                    reason="hook_error",
                )
            if not decision.allow:
                return ToolResult.error(
                    f"hook_denied: {decision.reason}",
                    reason="hook_denied",
                )
            override = decision.metadata.get("args_override")
            if isinstance(override, dict):
                script_args = override
                pre_hook = PreScriptUseHook(
                    skill_id=skill_id,
                    script_name=script_name,
                    language=descriptor.language,
                    args=dict(script_args),
                    timeout_seconds=descriptor.timeout_seconds,
                )

    # ---- 阶段 6：PermissionPolicy.check ----
    if permission_policy is not None:
        perm_request = PermissionRequest.for_script_exec(
            descriptor.full_target,
            script_args,
            thread_id=ctx.thread_id,
            submission_id=submission_id,
            entry_skill_id=entry_skill_id,
            call_chain=call_chain,
            turn_index=turn_index,
            extra_metadata=request_metadata,
        )
        perm_decision = await permission_policy.check(perm_request)
        if not perm_decision.granted:
            return ToolResult.error(
                f"permission_denied: {perm_decision.reason}",
                reason="permission_denied",
            )

    # ---- 阶段 7：执行 ----
    invocation = ScriptInvocation(
        descriptor=descriptor,
        args=script_args,
        cancel=ctx.cancel.child(f"script:{descriptor.full_target}"),
    )
    started_at = time.monotonic()
    await _emit_event(
        ctx,
        "script_execution_started",
        {
            "skill_id": skill_id,
            "script_name": script_name,
            "language": descriptor.language,
        },
    )
    try:
        result = await executor.execute(invocation)
    except ScriptExecutionError as e:
        await _emit_event(
            ctx,
            "script_execution_failed",
            {
                "skill_id": skill_id,
                "script_name": script_name,
                "exit_code": -1,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "stderr_excerpt": str(e),
            },
        )
        return ToolResult.error(
            f"script_execution_error: {e}",
            reason="execution_error",
        )

    # ---- 阶段 8：post_script_use hook（仅审计）----
    if hook_runner is not None:
        post_hook = _build_post_hook(descriptor, result)
        await hook_runner.run_audit_only("post_script_use", post_hook, hook_ctx)

    # ---- 阶段 9：打包 ToolResult + telemetry ----
    if result.is_timeout:
        await _emit_event(
            ctx,
            "script_execution_timeout",
            {
                "skill_id": skill_id,
                "script_name": script_name,
                "elapsed_ms": result.duration_ms,
            },
        )
    elif result.killed:
        await _emit_event(
            ctx,
            "script_execution_killed",
            {
                "skill_id": skill_id,
                "script_name": script_name,
                "elapsed_ms": result.duration_ms,
            },
        )
    elif result.exit_code != 0:
        await _emit_event(
            ctx,
            "script_execution_failed",
            {
                "skill_id": skill_id,
                "script_name": script_name,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stderr_excerpt": _truncate_preview(result.stderr, 256),
            },
        )
    else:
        await _emit_event(
            ctx,
            "script_execution_completed",
            {
                "skill_id": skill_id,
                "script_name": script_name,
                "duration_ms": result.duration_ms,
                "stdout_bytes": len(result.stdout.encode("utf-8")),
                "truncated": result.truncated,
            },
        )

    if result.ok:
        return ToolResult.ok(
            result.stdout,
            exit_code=0,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
            stderr=result.stderr,
        )

    error_kind = (
        "timeout"
        if result.is_timeout
        else "killed"
        if result.killed
        else f"exit_{result.exit_code}"
    )
    body = (
        f"exit={result.exit_code}"
        f"{' (timeout)' if result.is_timeout else ''}"
        f"{' (killed)' if result.killed and not result.is_timeout else ''}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return ToolResult.error(
        body,
        reason=error_kind,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        is_timeout=result.is_timeout,
        killed=result.killed,
        truncated=result.truncated,
        stdout=result.stdout,
    )


RUN_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {
            "type": "string",
            "description": "脚本所属 skill 的 id（通常是 caller 自己）。",
        },
        "script_name": {
            "type": "string",
            "description": "SKILL.md 中声明的 script 的 name。",
        },
        "args": {
            "type": "object",
            "description": "传给 script 的参数；按 script 的 args_schema 校验。",
            "additionalProperties": True,
        },
    },
    "required": ["skill_id", "script_name"],
    "additionalProperties": False,
}


def make_run_script_tool() -> ToolSpec:
    """构造 run_script ToolSpec。

    ``parallel_safe=False``：subprocess 有副作用，独占执行。
    """
    return ToolSpec(
        name="run_script",
        description=(
            "执行 SKILL.md 中声明的脚本。脚本必须在指定 skill 的 scripts 列表内。"
            "受 PermissionPolicy(scope=script_exec) 与 pre/post_script_use hook "
            "双重门控。"
        ),
        input_schema=RUN_SCRIPT_SCHEMA,
        handler=_run_script_handler,
        parallel_safe=False,
        timeout_seconds=600.0,
    )
