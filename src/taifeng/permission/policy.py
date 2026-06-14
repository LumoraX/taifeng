"""The PermissionPolicy aggregate -- rules + default mode + prompter + telemetry.

This is the top of the permission dependency graph. It wires together the rule
list (``rule``), the prompter protocol (``prompter``) and the value objects
(``models``), and exposes the ``check`` entry point the engine calls per request.

It also holds the telemetry callback type ``PolicyTelemetryCallback`` and the
constructors ``from_capability_tier`` / ``from_dict``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.permission.grant import GrantStore
from taifeng.permission.models import (
    CapabilityTier,
    PermissionDecision,
    PermissionGrant,
    PermissionMode,
    PermissionRequest,
)
from taifeng.permission.rule import PermissionRule
from taifeng.suspend.signal import SuspendSignal

if TYPE_CHECKING:
    # Type-only: PermissionPrompter appears only in annotations (the field/params are
    # accessed by attribute at runtime, never by name), so it stays out of runtime.
    from taifeng.permission.prompter import PermissionPrompter

logger = logging.getLogger(__name__)


PolicyTelemetryCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Callback the PermissionPolicy uses to emit telemetry events.

Signature: ``async def cb(event_kind: str, payload: dict) -> None``.
Event kinds: ``permission_prompt_timeout`` / ``permission_grant_issued`` /
``permission_grant_hit`` / ``permission_grant_expired``.
"""


@dataclass
class PermissionPolicy:
    """The full permission policy = rule list + default mode + optional prompter +
    prompter timeout.

    Check order:
        1. iterate rules; the first that matches returns its mode.
        2. no match -> default_mode.
        3. if mode == ``ask`` and prompter is set -> call the prompter (wrapped with
           prompter_timeout_seconds).
        4. ``ask`` but no prompter -> deny by default (conservative).
    """

    rules: list[PermissionRule] = field(default_factory=list)
    default_mode: PermissionMode = "ask"
    prompter: PermissionPrompter | None = None

    prompter_timeout_seconds: float = 0
    """Prompter timeout in seconds. 0 = no timeout (default; preserves original
    behaviour); >0 wraps the call with anyio.fail_after."""

    telemetry: PolicyTelemetryCallback | None = None
    """Telemetry callback injected by the business layer; used to emit the
    ``permission_prompt_timeout`` event."""

    # Set of one-shot pre-approved call_ids (injected by the engine on resume; a hit in
    # check consumes it and grants). Held via field(default_factory=set); it does not
    # participate in from_dict / from_capability_tier construction (they call
    # cls(rules=..., default_mode=..., prompter=...) without this field, so the
    # default_factory backs it with an empty set and behaviour is unchanged).
    _preapproved_call_ids: set[str] = field(default_factory=set)

    # Reusable approval grants (permission-grants). Like _preapproved_call_ids this is
    # kernel-held permission state that does not participate in from_dict /
    # from_capability_tier construction (default_factory backs it with an empty store,
    # so existing callers are unchanged). A grant is a "pre-answered ask": it bypasses
    # the prompter on a matching ask, but never overrides a deny rule (see check()).
    _grants: GrantStore = field(default_factory=GrantStore)

    def issue_grant(self, grant: PermissionGrant) -> PermissionGrant:
        """Register a reusable grant (also the resume re-seed entry point).

        Future ``ask``-mode requests matching this grant are auto-allowed without
        prompting (until exhausted via ``max_uses`` or revoked). It never overrides a
        ``deny`` rule -- a grant only caches the "yes" a human would have clicked.

        Args:
            grant: the grant to register. If ``grant_id`` is empty, a deterministic id
                is assigned.

        Returns:
            The effective stored grant (with its final ``grant_id``).
        """
        return self._grants.add(grant)

    def revoke_grant(self, grant_id: str) -> bool:
        """Revoke a previously issued grant by id (e.g. userspace wall-clock TTL).

        Returns:
            True if a grant was removed; False if no such id existed.
        """
        return self._grants.revoke(grant_id)

    def preapprove(self, call_id: str) -> None:
        """Register a one-shot pre-approval: the next check for this call_id grants
        directly (no prompt).

        Used for resume: a human has already approved a suspended tool call via Resume;
        before the engine re-runs that call it invokes this method so the prompter is
        not triggered again (otherwise a SuspendingPrompter would suspend forever).

        Args:
            call_id: the approved suspended tool call id (matches
                PermissionRequest.metadata['call_id']).
        """
        self._preapproved_call_ids.add(call_id)

    @classmethod
    def from_capability_tier(
        cls,
        tier: CapabilityTier,
        *,
        prompter: PermissionPrompter | None = None,
        prompter_timeout_seconds: float = 0,
    ) -> PermissionPolicy:
        """Build a policy from the coarse-grained capability ladder (G5d).

        The ladder is just a convenience starting point for common scenarios; it
        expands into the existing scope rules (consistent with the per-builtin checks),
        and the business layer can keep appending finer rules on the returned ``rules``
        to override:

        - ``read_only``: allow reads (``file_read``) only; write/exec/network all denied;
          everything else deny.
        - ``workspace_write``: allow reads + writes (``file_read`` / ``file_write``);
          everything else (shell / network, etc.) falls to default_mode=``ask`` (no
          prompter -> conservative deny).
        - ``danger_full_access``: default_mode=``allow``, unrestricted (high trust / dev).

        Raises:
            ValueError: unknown tier.
        """
        rules: list[PermissionRule]
        default_mode: PermissionMode
        if tier == "read_only":
            rules = [
                PermissionRule(scope="file_read", target_pattern="glob:*", mode="allow"),
            ]
            default_mode = "deny"
        elif tier == "workspace_write":
            rules = [
                PermissionRule(scope="file_read", target_pattern="glob:*", mode="allow"),
                PermissionRule(scope="file_write", target_pattern="glob:*", mode="allow"),
            ]
            default_mode = "ask"
        elif tier == "danger_full_access":
            rules = []
            default_mode = "allow"
        else:
            raise ValueError(
                f"unknown_capability_tier: {tier!r} "
                "(expected read_only / workspace_write / danger_full_access)"
            )
        return cls(
            rules=rules,
            default_mode=default_mode,
            prompter=prompter,
            prompter_timeout_seconds=prompter_timeout_seconds,
        )

    @classmethod
    def from_dict(
        cls,
        config: dict[str, Any],
        *,
        prompter: PermissionPrompter | None = None,
        telemetry: PolicyTelemetryCallback | None = None,
        prompter_timeout_seconds: float = 0,
    ) -> PermissionPolicy:
        """Build a PermissionPolicy from a dict / JSON config.

        Two auto-detected structures are supported (**must not be mixed**):

        **Style A -- Claude Code-style syntactic sugar**::

            {
                "default_mode": "ask",      # optional, defaults to "ask"
                "allow": ["Bash(openspec --help)", "Skill(read_*)"],
                "deny":  ["Bash(rm -rf *)"],
                "ask":   ["Bash(*)"],
            }

        **Style B -- explicit rule list**::

            {
                "default_mode": "allow",
                "rules": [
                    {"scope": "tool_use", "target_pattern": "shell_exec",
                     "args_match": {"cmd": "re:^openspec\\\\s"},
                     "mode": "allow", "reason": "ops_safe"},
                ],
            }

        In Style A, deny -> allow -> ask are expanded into rules in that order (deny
        first so it short-circuits).

        Raises:
            ValueError: mixing the two styles, unknown alias, syntax error, missing field.
        """
        default_mode = config.get("default_mode", "ask")
        if default_mode not in ("allow", "deny", "ask"):
            raise ValueError(
                f"invalid_default_mode: {default_mode!r} "
                "(expected allow / deny / ask)"
            )

        has_style_a = any(k in config for k in ("allow", "deny", "ask"))
        has_style_b = "rules" in config
        if has_style_a and has_style_b:
            raise ValueError(
                "permission_config_conflict: cannot mix Style A "
                "(allow/deny/ask) with Style B (rules)"
            )

        rules: list[PermissionRule] = []
        if has_style_a:
            # deny first: placed at the head of the rules list, short-circuits on a hit.
            for s in config.get("deny", []) or []:
                rules.append(PermissionRule.parse(s, mode="deny"))
            for s in config.get("allow", []) or []:
                rules.append(PermissionRule.parse(s, mode="allow"))
            for s in config.get("ask", []) or []:
                rules.append(PermissionRule.parse(s, mode="ask"))
        elif has_style_b:
            for obj in config.get("rules", []) or []:
                if not isinstance(obj, dict):
                    raise ValueError(
                        f"invalid_rule_object: {obj!r} (expected dict)"
                    )
                rules.append(PermissionRule(**obj))

        return cls(
            rules=rules,
            default_mode=default_mode,
            prompter=prompter,
            telemetry=telemetry,
            prompter_timeout_seconds=prompter_timeout_seconds,
        )

    async def check(self, request: PermissionRequest) -> PermissionDecision:
        # 0. One-shot pre-approval: a hit consumes it and grants (resume already has
        #    human approval, do not prompt again). Must run before rules/prompter,
        #    otherwise a SuspendingPrompter would suspend again into a dead loop.
        call_id = request.metadata.get("call_id")
        if call_id is not None and call_id in self._preapproved_call_ids:
            self._preapproved_call_ids.discard(call_id)
            return PermissionDecision.allow(reason="resume_preapproved")

        # 1+2. Rule lookup.
        mode: PermissionMode = self.default_mode
        rule_reason = ""
        for rule in self.rules:
            if rule.matches(request):
                mode = rule.mode
                rule_reason = rule.reason or f"matched rule: {rule.target_pattern}"
                break

        if mode == "allow":
            return PermissionDecision.allow(reason=rule_reason or "policy_allow")
        if mode == "deny":
            return PermissionDecision.deny(reason=rule_reason or "policy_deny")

        # ask
        # 2.5 grant short-circuit: a reusable grant is a "pre-answered ask". It runs
        #     only here -- AFTER deny/allow rules -- so a grant can bypass the prompt
        #     but never override an explicit deny rule (the security invariant).
        grant_decision = await self._try_grant(request)
        if grant_decision is not None:
            return grant_decision

        if self.prompter is None:
            return PermissionDecision.deny(
                reason="ask_mode_no_prompter (conservative default deny)",
            )
        try:
            decision = await self._invoke_prompter(request)
        except TimeoutError:
            # Timeout branch: emit telemetry + return deny.
            timeout_reason = f"prompter_timeout_{self.prompter_timeout_seconds}s"
            logger.warning(
                "permission prompter timed out after %ss for scope=%s target=%s",
                self.prompter_timeout_seconds,
                request.scope,
                request.target,
            )
            await self._emit_telemetry(
                "permission_prompt_timeout",
                {
                    "scope": request.scope,
                    "target": request.target,
                    "timeout_seconds": self.prompter_timeout_seconds,
                    "call_chain": list(request.call_chain),
                    "thread_id": request.thread_id,
                    "submission_id": request.submission_id,
                },
            )
            return PermissionDecision.deny(reason=timeout_reason)
        except SuspendSignal:
            # A suspend signal is not an error: it must pass through the wide ask-mode
            # except and be caught by dispatch_batch as a suspension.
            raise
        except Exception as e:
            logger.exception("prompter raised")
            return PermissionDecision.deny(reason=f"prompter_error: {e}")

        # permission-rule-args-match: the Taifeng kernel no longer auto-writes back to
        # rules on remember="always" (R1, business zero-intrusion: the kernel holds no
        # session permission state). If the business layer wants session/persistent
        # memory, it reads decision.remember_until in its prompter wrapper and manages
        # it itself:
        #     async def wrap(req):
        #         d = await inner.prompt(req)
        #         if d.remember_until == "always":
        #             business_store.persist(req, d)
        #         return d

        # Record a grant the prompter minted alongside its decision, so future
        # matching ask requests are auto-allowed (R5: re-seedable on resume).
        await self._record_minted_grant(decision, request)
        return decision

    async def _try_grant(
        self, request: PermissionRequest
    ) -> PermissionDecision | None:
        """Match the request against the GrantStore; on a hit consume + emit and
        return an allow decision, otherwise None (fall through to the prompter).
        """
        grant = self._grants.match(request)
        if grant is None:
            return None
        expired = self._grants.consume(grant)
        await self._emit_telemetry(
            "permission_grant_hit", self._grant_payload(grant, request)
        )
        if expired:
            # max_uses 用尽：grant 已移除，补一条失效事件供审计。
            await self._emit_telemetry(
                "permission_grant_expired", self._grant_payload(grant, request)
            )
        return PermissionDecision.allow(reason=f"grant:{grant.grant_id}")

    async def _record_minted_grant(
        self, decision: PermissionDecision, request: PermissionRequest
    ) -> None:
        """Register ``decision.grant`` (if any) into the store + emit an issued event."""
        if decision.grant is None:
            return
        issued = self._grants.add(decision.grant)
        await self._emit_telemetry(
            "permission_grant_issued", self._grant_payload(issued, request)
        )

    @staticmethod
    def _grant_payload(
        grant: PermissionGrant, request: PermissionRequest
    ) -> dict[str, Any]:
        """Build the telemetry payload for a grant event (audit-traceable)."""
        return {
            "grant_id": grant.grant_id,
            "scope": grant.scope,
            "target_pattern": grant.target_pattern,
            "reason": grant.reason,
            "call_chain": list(request.call_chain),
            "thread_id": request.thread_id,
            "submission_id": request.submission_id,
        }

    async def _invoke_prompter(self, request: PermissionRequest) -> PermissionDecision:
        """Call the prompter; whether to wrap with fail_after depends on
        prompter_timeout_seconds.

        Raises:
            TimeoutError: on timeout (only possible when prompter_timeout_seconds > 0).
        """
        assert self.prompter is not None  # guaranteed by the check() entry point
        if self.prompter_timeout_seconds and self.prompter_timeout_seconds > 0:
            # anyio.fail_after: raises TimeoutError on timeout and cancels the await
            # inside the prompter.
            with anyio.fail_after(self.prompter_timeout_seconds):
                return await self.prompter.prompt(request)
        # No-timeout path: no performance regression, identical to original behaviour.
        return await self.prompter.prompt(request)

    async def _emit_telemetry(self, kind: str, payload: dict[str, Any]) -> None:
        """Safely emit telemetry -- any exception is swallowed (does not affect the main flow)."""
        if self.telemetry is None:
            return
        try:
            await self.telemetry(kind, payload)
        except Exception:
            logger.exception("permission telemetry callback raised (ignored)")
