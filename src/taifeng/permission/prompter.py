"""The PermissionPrompter protocol and its built-in implementations.

The prompter is how the business layer answers an ``ask``-mode request:

    - ``PermissionPrompter`` -- the protocol the business layer implements.
    - ``CliPrompter`` -- a terminal y/n prompt (local dev / monkeypatched in tests).
    - ``CallbackPrompter`` -- an async-callback wrapper (Web / Slack, etc.).
    - ``SuspendingPrompter`` -- never returns; raises SuspendSignal so the turn unwinds
      and suspends, waiting for an out-of-band approval.

Depends only on ``models`` (PermissionRequest / PermissionDecision).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from taifeng.permission.models import PermissionDecision, PermissionRequest


@runtime_checkable
class PermissionPrompter(Protocol):
    """The ``ask``-mode implementation supplied by the business layer.

    The engine calls ``prompt`` and blocks waiting for the user's response. The
    business layer is responsible for:
        - CLI style: a terminal y/n prompt.
        - Web SSE style: push the request to the front end + await a future.
        - Slack/Discord style: send a message + let a bot await the reply.
    """

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        ...


PromptCallback = Callable[[PermissionRequest], Awaitable[PermissionDecision]]


class CliPrompter(PermissionPrompter):
    """A CLI-style prompter -- asks y/n over stdin/stdout.

    Suitable for local development / monkeypatch replacement in pytest.

    Note: the ``always-allow`` / ``always-deny`` options still return a
    PermissionDecision with ``remember="always"``, but since
    ``permission-rule-args-match`` the taifeng kernel **no longer** auto-persists
    that decision into policy.rules (R1, business zero-intrusion). If the business
    layer wants "click once, always allow, never ask again" semantics, it reads
    ``decision.remember_until`` in its own prompter wrapper and manages
    policy.rules.append or a DB write itself.
    """

    def __init__(
        self,
        *,
        input_func: Callable[[str], str] | None = None,
        output_func: Callable[[str], None] = print,
    ) -> None:
        self._input = input_func or input
        self._output = output_func

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self._output(f"\n[Permission Required] {request.scope}")
        self._output(f"  target: {request.target}")
        if request.reason:
            self._output(f"  reason: {request.reason}")
        if request.thread_id or request.entry_skill_id:
            self._output(
                f"  ctx:    thread={request.thread_id} entry={request.entry_skill_id} "
                f"chain={list(request.call_chain)}"
            )
        if request.metadata:
            self._output(f"  meta:   {request.metadata}")
        choice = self._input("Allow? [y/N/always-allow/always-deny] ").strip().lower()
        if choice in ("y", "yes"):
            return PermissionDecision.allow(reason="user_y")
        if choice in ("always-allow", "aa", "a"):
            return PermissionDecision.allow(reason="user_always", remember="always")
        if choice in ("always-deny", "ad"):
            return PermissionDecision.deny(reason="user_always_deny", remember="always")
        return PermissionDecision.deny(reason="user_n")


class CallbackPrompter(PermissionPrompter):
    """An async-callback-style prompter -- a Web/Slack business-layer wrapper."""

    def __init__(self, callback: PromptCallback) -> None:
        self._callback = callback

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        return await self._callback(request)


class SuspendingPrompter(PermissionPrompter):
    """A suspending prompter -- ``ask`` mode does not block; it raises SuspendSignal so
    the turn unwinds and suspends.

    Mirrors openclaw's "tool returns approval-pending early"; the difference is that
    taifeng does not return a status code but raises a control-flow exception, which
    run_turn aggregates into a TurnSuspended.

    request_id is produced by an injected factory (fixable in tests); payload_schema
    gives the JSON Schema of the approval decision.
    """

    # JSON Schema of the approval decision -- the business layer / front end renders
    # the approval UI from this.
    _DECISION_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "granted": {"type": "boolean"},
            "reason": {"type": "string"},
            "remember_until": {"enum": ["once", "session", "always"]},
        },
        "required": ["granted"],
    }

    def __init__(self, *, request_id_factory: Callable[[], str] | None = None) -> None:
        """Initialise the SuspendingPrompter.

        Args:
            request_id_factory: an injectable id factory (fixable in tests); defaults
                to random generation.
        """
        # Injectable id factory (fixable in tests); defaults to random.
        self._gen_id = request_id_factory or (lambda: f"req_{secrets.token_hex(6)}")

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        """Never returns -- always raises SuspendSignal(reason=PERMISSION).

        Raises:
            SuspendSignal: always, so the turn unwinds and suspends pending approval.
        """
        # Lazy import to avoid a circular dependency (the suspend package must not
        # depend on the permission package).
        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.signal import SuspendSignal

        # detail: pass the approval context through to the front end (R1: taifeng does
        # not parse it, only carries it).
        detail: dict[str, Any] = {
            "scope": request.scope,
            "target": request.target,
            "reason": request.reason,
            "call_chain": list(request.call_chain),
            **request.metadata,
        }
        pending = PendingRequest(
            request_id=self._gen_id(),
            reason=SuspendReason.PERMISSION,
            payload_schema=self._DECISION_SCHEMA,
            related_call_id=request.metadata.get("call_id"),
            detail=detail,
        )
        raise SuspendSignal(pending)
