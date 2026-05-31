"""Hooks 类型定义。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


HookKind = Literal[
    "pre_tool_use",
    "post_tool_use",
    "pre_compact",
    "pre_turn",
    "pre_skill_dispatch",
    "post_skill_dispatch",
    "pre_script_use",
    "post_script_use",
]


@dataclass(frozen=True)
class HookContext:
    """钩子上下文 —— 业务层数据透传。"""

    thread_id: str
    submission_id: str
    entry_skill_id: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookDecision:
    """钩子返回的决策。"""

    allow: bool = True
    reason: str | None = None
    """拒绝原因。仅 allow=False 时使用。"""

    metadata: dict[str, Any] = field(default_factory=dict)
    """钩子可附加元数据；后续钩子 / telemetry 可读。"""

    @classmethod
    def ok(cls, **metadata: Any) -> HookDecision:
        return cls(allow=True, metadata=metadata)

    @classmethod
    def deny(cls, reason: str, **metadata: Any) -> HookDecision:
        return cls(allow=False, reason=reason, metadata=metadata)


# === 各钩子的输入数据 ===


@dataclass(frozen=True)
class PreToolUseHook:
    """工具执行前。

    G5a：返回 ``HookDecision.deny`` 中止派发；放行时若
    ``decision.metadata["args_override"]`` 为 dict，则替换原 args 进入 dispatch
    与 PostToolUse（与 ``pre_script_use`` 的 args_override 对齐）。
    """

    tool_name: str
    arguments: dict[str, Any]
    parallel_safe: bool
    call_id: str


@dataclass(frozen=True)
class PostToolUseHook:
    """工具执行后。"""

    tool_name: str
    arguments: dict[str, Any]
    output: str
    is_error: bool
    duration_ms: int
    call_id: str


@dataclass(frozen=True)
class PreCompactHook:
    """压缩触发前。"""

    phase: Literal["pre_turn", "mid_turn", "manual"]
    token_estimate: int
    history_length: int


@dataclass(frozen=True)
class PreTurnHook:
    """turn 启动前。"""

    user_text: str
    iteration: int


# 单次 PostSkillDispatchHook.output_preview 字节上限（防止 hook 数据过大）
SKILL_OUTPUT_PREVIEW_LIMIT = 1024


@dataclass(frozen=True)
class PreSkillDispatchHook:
    """子 skill 派发前 —— DispatchPolicy 通过、PermissionPolicy 未跑之前。

    业务侧用此 hook 实现"租户级 child skill 黑名单"等动态拦截。
    返回 ``HookDecision.deny`` 即可中止派发；后续 PermissionPolicy/sub_turn
    都不会执行。
    """

    target_skill_id: str
    """即将派发的 child skill。"""

    args: dict[str, Any]
    """传给子 skill 的参数（来自 LLM 的 call_skill tool 调用）。"""

    caller_skill_id: str
    """当前正在执行的 skill —— 等于 call_chain 的最后一个。"""

    call_chain: tuple[str, ...]
    """当前 skill 调用栈（含 caller）。"""

    depth: int
    """派发后将达到的栈深度（= len(call_chain) + 1）。"""


@dataclass(frozen=True)
class PostSkillDispatchHook:
    """子 skill 派发后 —— sub_turn 完成（成功 OR 失败）后的审计点。

    重要约束：本 hook **仅审计**，不能否决（spec 强制）。返回 deny 不会修改
    ToolResult；只会被记录到 telemetry。Hook 自身异常也不会影响 ToolResult。
    """

    target_skill_id: str
    """已派发执行的 child skill。"""

    caller_skill_id: str
    """发起派发的 skill。"""

    success: bool
    """子 turn 是否成功（False 时也会触发 —— 审计闭环）。"""

    duration_ms: int
    """子 turn 总耗时。"""

    sub_thread_id: str | None
    """子 turn 创建的 thread；None 表示未成功创建。"""

    output_preview: str
    """子 turn final_text 前 1KB 截断（spec: SKILL_OUTPUT_PREVIEW_LIMIT）。"""


# === Script lifecycle hooks ===


# 单次 PostScriptUseHook.stdout_preview / stderr_preview 字节上限
SCRIPT_OUTPUT_PREVIEW_LIMIT = 1024


@dataclass(frozen=True)
class PreScriptUseHook:
    """脚本执行前 —— DispatchPolicy 通过、PermissionPolicy 未跑之前。

    业务侧用此 hook 实现"基于 args 的动态拒绝" / "args 改写"等场景。
    返回 ``HookDecision.deny`` 即中止；``decision.metadata["args_override"]``
    若存在且为 dict，将替换原 args 进入后续阶段。
    """

    skill_id: str
    """所属 skill 的 id。"""

    script_name: str
    """被请求执行的 script name。"""

    language: str
    """``shell`` / ``python`` / ``custom``。"""

    args: dict[str, Any]
    """LLM 提供的 args（已通过 args_schema 校验）。"""

    timeout_seconds: float
    """声明的超时上限。"""


@dataclass(frozen=True)
class PostScriptUseHook:
    """脚本执行后 —— 不可否决，仅审计。

    与 ``post_skill_dispatch`` 同义：hook 返回 deny 或异常都不会修改 ToolResult。
    """

    skill_id: str
    script_name: str
    exit_code: int
    duration_ms: int
    is_timeout: bool
    killed: bool
    truncated: bool
    stdout_preview: str
    """stdout 前 ``SCRIPT_OUTPUT_PREVIEW_LIMIT`` 字节截断。"""

    stderr_preview: str
    """stderr 前 ``SCRIPT_OUTPUT_PREVIEW_LIMIT`` 字节截断。"""


# === 钩子处理器协议 ===


HookHandler = Callable[
    [Any, HookContext],  # (hook_data, ctx)
    Awaitable[HookDecision],
]


@runtime_checkable
class HookSink(Protocol):
    """单个钩子处理器协议。"""

    async def handle(self, hook: Any, ctx: HookContext) -> HookDecision:
        ...


# === 注册表 ===


class HookRegistry:
    """按 HookKind 分桶的钩子注册表。"""

    def __init__(self) -> None:
        self._handlers: dict[HookKind, list[HookHandler]] = {
            "pre_tool_use": [],
            "post_tool_use": [],
            "pre_compact": [],
            "pre_turn": [],
            "pre_skill_dispatch": [],
            "post_skill_dispatch": [],
            "pre_script_use": [],
            "post_script_use": [],
        }

    def register(self, kind: HookKind, handler: HookHandler) -> None:
        self._handlers[kind].append(handler)

    def handlers(self, kind: HookKind) -> list[HookHandler]:
        return list(self._handlers[kind])


# === Runner —— 串行执行所有钩子，遇拒绝立即停止 ===


class HookRunner:
    """串行运行钩子，任一钩子 deny → 整体 deny。"""

    def __init__(self, registry: HookRegistry | None = None) -> None:
        self._registry = registry or HookRegistry()

    @property
    def registry(self) -> HookRegistry:
        return self._registry

    async def run(self, kind: HookKind, hook: Any, ctx: HookContext) -> HookDecision:
        for handler in self._registry.handlers(kind):
            try:
                decision = await handler(hook, ctx)
            except Exception as e:
                logger.exception("hook %s raised", kind)
                return HookDecision.deny(f"hook_error: {e}")
            if not decision.allow:
                return decision
        return HookDecision.ok()

    async def run_audit_only(
        self,
        kind: HookKind,
        hook: Any,
        ctx: HookContext,
    ) -> None:
        """串行执行所有 hook，但 deny / 异常都被吞掉（仅审计；不影响调用方）。

        用途：``post_skill_dispatch`` 等"事后审计"场景 —— spec 强制 hook 不能改
        ToolResult；hook 自身异常仅写日志。
        """
        for handler in self._registry.handlers(kind):
            try:
                await handler(hook, ctx)
            except Exception:
                logger.exception("audit-only hook %s raised (ignored)", kind)
