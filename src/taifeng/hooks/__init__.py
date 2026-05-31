"""Hooks 系统 —— PreToolUse / PostToolUse / PreCompact / PreTurn /
PreSkillDispatch / PostSkillDispatch 钩子。

参照：
    - claw-code crates/plugins/src/hooks.rs（PreToolUse / PostToolUse）
    - codex pre_compact / post_compact 钩子

设计：所有钩子是 async + 可中止（返回 ``HookDecision.deny`` 即拒绝该操作）。
钩子不修改数据流；仅观察与决策。如需改写，业务侧自行包装 ToolHandler。

注：``post_skill_dispatch`` 是"事后审计" hook —— 返回 deny 不会改 ToolResult，
hook 自身异常也不影响主流程；只发 telemetry。详见 spec
``docs/architecture/capabilities/hooks.md``。
"""

from taifeng.hooks.types import (
    HookContext,
    HookDecision,
    HookHandler,
    HookKind,
    HookRegistry,
    HookRunner,
    PostScriptUseHook,
    PostSkillDispatchHook,
    PostToolUseHook,
    PreCompactHook,
    PreScriptUseHook,
    PreSkillDispatchHook,
    PreToolUseHook,
    PreTurnHook,
    SCRIPT_OUTPUT_PREVIEW_LIMIT,
    SKILL_OUTPUT_PREVIEW_LIMIT,
)

__all__ = [
    "HookContext",
    "HookDecision",
    "HookHandler",
    "HookKind",
    "HookRegistry",
    "HookRunner",
    "PostScriptUseHook",
    "PostSkillDispatchHook",
    "PostToolUseHook",
    "PreCompactHook",
    "PreScriptUseHook",
    "PreSkillDispatchHook",
    "PreToolUseHook",
    "PreTurnHook",
    "SCRIPT_OUTPUT_PREVIEW_LIMIT",
    "SKILL_OUTPUT_PREVIEW_LIMIT",
]
