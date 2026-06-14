"""Permission matching rules and the Claude Code-style rule-string parser.

This module owns everything about *matching* a request against a rule:

    - ``_match_pattern`` -- the tri-state matcher (literal / ``re:`` / ``glob:``).
    - ``PermissionRule`` -- scope + target pattern + optional args_match + decision mode.
    - ``PermissionRule.parse`` -- parses ``<Alias>(<payload>)`` rule strings, driven by
      ``_PERMISSION_ALIAS_TABLE``.

Depends only on ``models`` (PermissionScope / PermissionMode / PermissionRequest).
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only imports: all three names appear solely in annotations, which are
    # strings under ``from __future__ import annotations`` and never evaluated at runtime.
    from taifeng.permission.models import (
        PermissionMode,
        PermissionRequest,
        PermissionScope,
    )

logger = logging.getLogger(__name__)


def _match_pattern(pattern: str, value: str) -> bool:
    """Tri-state pattern match -- literal / ``re:`` regex / ``glob:`` wildcard.

    - ``"foo"`` -> strict equality.
    - ``"re:^foo.*"`` -> ``re.search`` match.
    - ``"glob:foo*"`` -> ``fnmatch.fnmatch`` match (``*`` / ``?`` / ``[seq]``).

    An invalid regex -> returns False + a warning log (conservatively no match, so a
    bad pattern never crashes the main flow).
    """
    if pattern.startswith("re:"):
        try:
            return bool(re.search(pattern[3:], value))
        except re.error:
            logger.warning("invalid regex in permission pattern: %r", pattern)
            return False
    if pattern.startswith("glob:"):
        return fnmatch.fnmatch(value, pattern[5:])
    return pattern == value


@dataclass(frozen=True)
class PermissionRule:
    """A matching rule -- scope + target pattern + optional args_match + decision mode.

    Pattern semantics follow ``_match_pattern``: literal / ``re:`` / ``glob:`` tri-state.
    Both ``target_pattern`` and the values in ``args_match`` use the same grammar.
    """

    scope: PermissionScope
    target_pattern: str
    """Matches ``request.target`` via literal / ``re:`` regex / ``glob:`` wildcard."""

    mode: PermissionMode
    reason: str = ""

    args_match: dict[str, str] | None = None
    """Optional: exact args-level matching. key = a field name inside
    request.metadata['args'], value = a pattern. AND semantics: every key must match;
    if any key is missing from args -> no match (conservative). None (default) -> skip
    the args check (backward compatible with existing rules)."""

    def matches(self, request: PermissionRequest) -> bool:
        if request.scope != self.scope:
            return False
        if not _match_pattern(self.target_pattern, request.target):
            return False
        # args_match: can only match when metadata carries an args dict.
        if self.args_match:
            md = request.metadata or {}
            args = md.get("args")
            if not isinstance(args, dict):
                return False
            for key, pattern in self.args_match.items():
                if key not in args:
                    return False
                value = str(args[key])
                if not _match_pattern(pattern, value):
                    return False
        return True

    @classmethod
    def parse(
        cls, rule_str: str, *, mode: PermissionMode, reason: str = "",
    ) -> PermissionRule:
        """Parse a Claude Code-style rule string into a PermissionRule.

        Supported grammar: ``<Alias>(<payload>)``; aliases live in
        ``_PERMISSION_ALIAS_TABLE``.

        Payload interpretation:
            - alias with args_key (Bash/FileRead/FileWrite/ShellExec): payload is the
              pattern for args[key]; target is fixed (e.g. "shell_exec").
            - alias without args_key (Skill/Script/ApplyPatch): payload is the
              target_pattern; args_match stays None.
            - empty payload / ``"*"`` -> match everything (target -> ``"glob:*"`` /
              args_match -> ``{key: "glob:*"}``).
            - containing ``*`` / ``?`` / ``[`` -> auto-prefixed with ``glob:`` (unless it
              already carries ``re:``).

        Raises:
            ValueError: syntax error / unknown alias / empty string.
        """
        m = _PERMISSION_RULE_SYNTAX_RE.match(rule_str.strip())
        if not m:
            raise ValueError(
                f"invalid_permission_syntax: {rule_str!r} "
                "(expected '<Alias>(<payload>)')"
            )
        alias, payload = m.group(1), m.group(2)
        spec = _PERMISSION_ALIAS_TABLE.get(alias)
        if spec is None:
            raise ValueError(
                f"unknown_permission_syntax: alias {alias!r} not in "
                f"{sorted(_PERMISSION_ALIAS_TABLE)}"
            )
        scope: PermissionScope = spec["scope"]
        fixed_target: str | None = spec.get("target")
        args_key: str | None = spec.get("args_key")

        normalized = _normalize_pattern(payload)

        if args_key:
            # tool_use scope + fixed target + args_match
            assert fixed_target is not None
            return cls(
                scope=scope,
                target_pattern=fixed_target,
                mode=mode,
                reason=reason or f"parsed:{rule_str}",
                args_match={args_key: normalized},
            )
        # No args_key: payload is the target_pattern.
        return cls(
            scope=scope,
            target_pattern=normalized,
            mode=mode,
            reason=reason or f"parsed:{rule_str}",
        )


# Regex parsing the ``<Alias>(<payload>)`` grammar.
_PERMISSION_RULE_SYNTAX_RE = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)


def _normalize_pattern(raw: str) -> str:
    """Normalise a user-supplied pattern on the parse path:

    - empty string / "*" -> ``"glob:*"`` (match everything).
    - already prefixed with ``re:`` or ``glob:`` -> left as-is.
    - containing glob wildcards (``*`` / ``?`` / ``[``) -> auto-prefixed with ``glob:``.
    - otherwise -> literal (no prefix).
    """
    if raw == "" or raw == "*":
        return "glob:*"
    if raw.startswith("re:") or raw.startswith("glob:"):
        return raw
    if any(ch in raw for ch in ("*", "?", "[")):
        return f"glob:{raw}"
    return raw


# Alias table for the Claude Code-style grammar -- each entry declares a scope, an
# optional fixed target and an optional args field name. To add an alias, add one row.
_PERMISSION_ALIAS_TABLE: dict[str, dict[str, Any]] = {
    # shell execution -- payload is the cmd
    "Bash":       {"scope": "tool_use", "target": "shell_exec", "args_key": "cmd"},
    "ShellExec":  {"scope": "tool_use", "target": "shell_exec", "args_key": "cmd"},
    # child skill dispatch -- payload is the target_pattern
    "Skill":      {"scope": "skill_dispatch"},
    # script execution (SKILL.md scripts) -- payload is the script_name pattern
    "Script":     {"scope": "script_exec"},
    # file IO -- payload is the path
    "FileRead":   {"scope": "tool_use", "target": "file_read",  "args_key": "path"},
    "FileWrite":  {"scope": "tool_use", "target": "file_write", "args_key": "path"},
    # structured patch -- payload is a path such as root_dir (no args_key yet, matched by target)
    "ApplyPatch": {"scope": "tool_use", "target": "apply_patch"},
}
