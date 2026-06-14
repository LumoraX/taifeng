"""Core permission data models: requests, decisions and shared aliases.

This module holds the leaf-level value objects of the permission gate, the
ones every other permission module depends on:

    - ``PermissionRequest``  -- an approval request handed to the business layer,
      carrying typed context fields plus an open ``metadata`` extension point.
    - ``PermissionDecision`` -- the approval outcome (granted / mode / memory window).
    - Shared type aliases (``PermissionMode`` / ``PermissionScope`` /
      ``CapabilityTier``) and the ``CALL_CHAIN_MAX_DEPTH`` bound.

It has no intra-package dependencies; ``rule`` / ``prompter`` / ``policy`` all
build on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Approval mode: allow outright / deny outright / ask the prompter.
PermissionMode = Literal["allow", "deny", "ask"]


# Coarse-grained capability ladder (ReadOnly < WorkspaceWrite < DangerFullAccess).
# A convenience starting point only; it expands into the existing scope rules and
# introduces no new mechanism (mirrors claw-code permissions.rs).
CapabilityTier = Literal["read_only", "workspace_write", "danger_full_access"]


PermissionScope = Literal[
    "tool_use",        # invoke a tool
    "file_read",       # read a file
    "file_write",      # write a file
    "shell_exec",      # execute a shell command
    "network",         # make a network request
    "compaction",      # trigger compaction
    "skill_dispatch",  # dispatch to a child skill
    "script_exec",     # run a script (Python / Bash / business scripts)
    "custom",
]


# Upper bound on a single PermissionRequest.call_chain depth -- anything deeper is
# truncated to the trailing N entries. With the default max_call_depth=6 this is
# never hit in practice; the cap is purely a guard that stops telemetry payloads
# from blowing up.
CALL_CHAIN_MAX_DEPTH = 32


@dataclass(frozen=True)
class PermissionRequest:
    """An approval request delivered to the business layer.

    Required positional fields (kept for backward compatibility): ``scope`` /
    ``target``.

    Typed context fields (all keyword-only with defaults, so legacy callers keep
    working):
        - ``thread_id``: the owning conversation / thread; used by the business
          layer for auditing and UI rendering.
        - ``submission_id``: the current Submission; disambiguates multiple turns
          within one thread.
        - ``entry_skill_id``: the entry skill of the whole call graph.
        - ``call_chain``: the skill call stack, deepest last; truncated past 32.
        - ``turn_index``: the parent turn's iteration index (1-based).

    Open extension: ``metadata: dict`` is owned by the business layer (opaque;
    taifeng never parses its keys). Any domain context the business side needs to
    pass through (custom routing keys, etc.) goes entirely into ``metadata`` -- the
    engine only carries it verbatim to the prompter / telemetry and never adds a
    business-named field of its own (R1).
    """

    scope: PermissionScope
    """The permission scope."""

    target: str
    """The approval target -- tool name / file path / shell command / skill_id, etc."""

    reason: str = ""
    """Why the permission is needed (rationale from the LLM / filled by the business layer)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra context: business-defined fields.

    Important: this field is the open extension point. taifeng's internal handlers
    have moved to constructing the typed fields; the business layer only needs
    ``metadata`` for its own custom scenarios.
    """

    # ----- typed context fields (keyword-only via dataclass defaults) -----
    thread_id: str = ""
    """Owning thread; empty string means not provided."""

    submission_id: str = ""
    """Current Submission; empty string means not provided."""

    entry_skill_id: str = ""
    """Entry skill; empty string means not provided."""

    call_chain: tuple[str, ...] = ()
    """Skill call stack, deepest last. Truncated to the tail past CALL_CHAIN_MAX_DEPTH."""

    turn_index: int = 0
    """Parent turn's iteration index (1-based; 0 means not provided)."""

    def __post_init__(self) -> None:
        # Truncate call_chain to the trailing N entries when it exceeds the cap
        # (keeps the most recent call stack).
        if len(self.call_chain) > CALL_CHAIN_MAX_DEPTH:
            # A frozen dataclass requires object.__setattr__ for internal assignment.
            object.__setattr__(
                self,
                "call_chain",
                tuple(self.call_chain[-CALL_CHAIN_MAX_DEPTH:]),
            )

    # ------------------------------------------------------------------
    # Factory methods -- required keyword-only args force the business layer
    # to pass context.
    # ------------------------------------------------------------------

    @classmethod
    def for_tool_call(
        cls,
        tool_name: str,
        args: dict[str, Any],
        *,
        thread_id: str,
        submission_id: str,
        entry_skill_id: str,
        turn_index: int,
        call_chain: tuple[str, ...] = (),
        extra_metadata: dict[str, Any] | None = None,
        reason: str = "",
    ) -> PermissionRequest:
        """Build a "tool call" approval request.

        Args:
            tool_name: tool name (e.g. ``shell_exec``).
            args: tool arguments (only the first 200 chars are serialised into metadata).
            thread_id / submission_id / entry_skill_id / turn_index: required context.
            call_chain: current skill call stack (default empty = on the entry skill).
            extra_metadata: opaque business context, merged verbatim into ``metadata``
                (taifeng does not parse it).
            reason: optional approval reason (rationale from the LLM).

        Returns:
            PermissionRequest with scope='tool_use'.
        """
        return cls(
            scope="tool_use",
            target=tool_name,
            reason=reason,
            metadata={"args": args, **(extra_metadata or {})},
            thread_id=thread_id,
            submission_id=submission_id,
            entry_skill_id=entry_skill_id,
            call_chain=call_chain,
            turn_index=turn_index,
        )

    @classmethod
    def for_script_exec(
        cls,
        script_name: str,
        args: dict[str, Any],
        *,
        thread_id: str,
        submission_id: str,
        entry_skill_id: str,
        call_chain: tuple[str, ...],
        turn_index: int,
        extra_metadata: dict[str, Any] | None = None,
        reason: str = "",
    ) -> PermissionRequest:
        """Build a "script execution" approval request (Bash / Python / custom scripts).

        ``extra_metadata`` is opaque business context, merged verbatim into
        ``metadata`` (taifeng does not parse it).

        Returns:
            PermissionRequest with scope='script_exec'.
        """
        return cls(
            scope="script_exec",
            target=script_name,
            reason=reason,
            metadata={"args": args, **(extra_metadata or {})},
            thread_id=thread_id,
            submission_id=submission_id,
            entry_skill_id=entry_skill_id,
            call_chain=call_chain,
            turn_index=turn_index,
        )

    @classmethod
    def for_skill_dispatch(
        cls,
        target_skill_id: str,
        *,
        caller_skill_id: str,
        call_chain: tuple[str, ...],
        thread_id: str,
        submission_id: str,
        entry_skill_id: str,
        turn_index: int,
        extra_metadata: dict[str, Any] | None = None,
        reason: str = "",
    ) -> PermissionRequest:
        """Build a "child skill dispatch" approval request.

        Args:
            target_skill_id: the child skill about to be dispatched.
            caller_skill_id: the currently executing skill (the last entry of call_chain).
            call_chain: current skill call stack.
            extra_metadata: opaque business context, merged verbatim into ``metadata``
                (taifeng does not parse it).
            the rest: required context.

        Returns:
            PermissionRequest with scope='skill_dispatch'.
        """
        return cls(
            scope="skill_dispatch",
            target=target_skill_id,
            reason=reason,
            metadata={"caller_skill_id": caller_skill_id, **(extra_metadata or {})},
            thread_id=thread_id,
            submission_id=submission_id,
            entry_skill_id=entry_skill_id,
            call_chain=call_chain,
            turn_index=turn_index,
        )


@dataclass(frozen=True)
class PermissionDecision:
    """The approval outcome."""

    granted: bool
    mode: PermissionMode
    reason: str = ""
    remember_until: Literal["once", "session", "always"] | None = None
    """Decision memory window -- ``once``=this call only; ``session``=this session;
    ``always``=persisted into the policy permanently."""

    @classmethod
    def allow(
        cls, *, reason: str = "", remember: Literal["once", "session", "always"] = "once"
    ) -> PermissionDecision:
        return cls(granted=True, mode="allow", reason=reason, remember_until=remember)

    @classmethod
    def deny(
        cls, *, reason: str, remember: Literal["once", "session", "always"] = "once"
    ) -> PermissionDecision:
        return cls(granted=False, mode="deny", reason=reason, remember_until=remember)
