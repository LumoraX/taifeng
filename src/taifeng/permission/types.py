"""Backward-compatible aggregator for the permission gate types.

The permission types used to live in this single module. They have since been
split by responsibility into:

    - ``models``   -- PermissionRequest / PermissionDecision + shared aliases.
    - ``rule``     -- PermissionRule + the rule-string parser.
    - ``prompter`` -- PermissionPrompter protocol + CLI / Callback / Suspending impls.
    - ``policy``   -- PermissionPolicy + the telemetry callback type.

This module is kept as the stable public import path: ``from taifeng.permission.types
import X`` keeps working for every existing call site. New code may import from the
focused modules above or from the ``taifeng.permission`` package directly.
"""

from __future__ import annotations

from taifeng.permission.grant import GrantStore
from taifeng.permission.models import (
    CALL_CHAIN_MAX_DEPTH,
    CapabilityTier,
    PermissionDecision,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
)
from taifeng.permission.policy import PermissionPolicy, PolicyTelemetryCallback
from taifeng.permission.prompter import (
    CallbackPrompter,
    CliPrompter,
    PermissionPrompter,
    PromptCallback,
    SuspendingPrompter,
)
from taifeng.permission.rule import PermissionRule

__all__ = [
    "CALL_CHAIN_MAX_DEPTH",
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
    "PolicyTelemetryCallback",
    "PromptCallback",
    "SuspendingPrompter",
]
