"""Permission gate —— HITL 审批协议 + 自动策略。

参照：
    - claw-code crates/runtime/src/permission_enforcer.rs
    - codex codex-rs/core/src/agent/* approval flow

设计：
    - 不内置 UI；通过 ``PermissionPrompter`` 协议交给业务层（CLI 弹框 / Web SSE / Slack）
    - 引擎层只发起请求 + 等结果
    - 4 种策略：always_allow / always_deny / always_ask / auto（按规则）
"""

from taifeng.permission.types import (
    CallbackPrompter,
    CapabilityTier,
    CliPrompter,
    GrantStore,
    PermissionDecision,
    PermissionGrant,
    PermissionMode,
    PermissionPolicy,
    PermissionPrompter,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
    PromptCallback,
    SuspendingPrompter,
)

__all__ = [
    "CallbackPrompter",
    "CapabilityTier",
    "CliPrompter",
    "GrantStore",
    "PermissionDecision",
    "PermissionGrant",
    "PermissionMode",
    "PermissionPolicy",
    "PermissionPrompter",
    "PermissionRequest",
    "PermissionRule",
    "PermissionScope",
    "PromptCallback",
    "SuspendingPrompter",
]
