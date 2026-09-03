"""权限匹配规则与 Claude Code 风格规则串解析器。

本模块负责「请求 vs 规则」的匹配：

    - ``_match_pattern`` —— 三态匹配（字面 / ``re:`` 正则 / ``glob:`` 通配）
    - ``PermissionRule`` —— scope + target_pattern + 可选 args_match + 决策模式
    - ``PermissionRule.parse`` —— 解析 ``<Alias>(<payload>)`` 规则串，由
      ``_PERMISSION_ALIAS_TABLE`` 驱动

权限模型是**效果模型**（ADR 0028）：scope 表达做了什么类型的事（shell_exec /
file_read / file_write / network / script_exec / skill_dispatch），target 是规范化后的
作用对象；``tool_use`` 只是无更细效果的工具的兜底 scope。Style A 别名一律映射到
效果 scope；``args_match`` 保留给 Style B 针对 ``tool_use`` 的规则。

只依赖 ``models``（PermissionScope / PermissionMode / PermissionRequest）。
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
        """解析 Claude Code 风格规则串为 PermissionRule（效果模型，ADR 0028）。

        语法 ``<Alias>(<payload>)``，别名表见 ``_PERMISSION_ALIAS_TABLE``。每个别名映射到
        一个**效果 scope**，payload 即该 scope 的 ``target_pattern``（经 ``_normalize_pattern``
        归一：空 / ``*`` → ``glob:*``；含通配 → 自动加 ``glob:``；``re:`` / ``glob:`` 原样）。
        Style A 不产出 ``args_match``——那是 Style B 针对 ``tool_use`` 兜底 scope 的工具。

        ``Network(p)`` 特例：target 形状是 ``"<METHOD> <URL>"``，payload 若未指定 method
        （不以 ``re:`` 开头且首 token 不是 HTTP method），先前缀 ``"* "`` 再归一，
        使任意 method 命中。

        Raises:
            ValueError: 语法错误 / 未知别名。
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
        normalize = spec.get("normalize", _normalize_pattern)
        return cls(
            scope=scope,
            target_pattern=normalize(payload),
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


# network scope 的 target 是 "<METHOD> <URL>"；这些 token 视为已指定 method。
_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
)


def _normalize_network_pattern(raw: str) -> str:
    """``Network(p)`` 的归一：未指定 method 时前缀 ``"* "`` 使任意 method 命中。

    - ``re:`` 开头 → 原样透传（作者自行处理 method 前缀）
    - 空 / ``*`` → ``glob:*``
    - 首 token ∈ HTTP methods（如 ``GET https://a/*``）→ 按字面归一
    - 其余（如 ``https://a/*`` / ``https://a/x``）→ ``glob:* <p>``
      （字面 URL 也走 glob，因为 ``*`` 前缀本身就是通配）
    """
    if raw.startswith("re:") or raw in ("", "*"):
        return _normalize_pattern(raw)
    body = raw[5:] if raw.startswith("glob:") else raw
    first = body.split(" ", 1)[0]
    if first.upper() in _HTTP_METHODS:
        return _normalize_pattern(raw)
    return f"glob:* {body}"


# Style A 别名表（效果模型，ADR 0028）：每个别名 = 一个效果 scope，payload 即
# target_pattern。新增别名只需加一行；可选 ``normalize`` 覆盖默认归一函数。
_PERMISSION_ALIAS_TABLE: dict[str, dict[str, Any]] = {
    # shell 执行 —— payload 匹配完整命令串（shell_exec / run_in_background 同 scope）
    "Bash":       {"scope": "shell_exec"},
    "ShellExec":  {"scope": "shell_exec"},
    # 子 skill 派发 —— payload 匹配目标 skill id
    "Skill":      {"scope": "skill_dispatch"},
    # SKILL.md 脚本 —— payload 匹配 "<skill_id>/<script_name>"
    "Script":     {"scope": "script_exec"},
    # 文件 IO —— payload 匹配解析后的绝对路径
    "FileRead":   {"scope": "file_read"},
    "FileWrite":  {"scope": "file_write"},
    # 网络 —— payload 匹配 "<METHOD> <URL>"，省略 method 时任意 method 命中
    "Network":    {"scope": "network", "normalize": _normalize_network_pattern},
    # 结构化补丁 —— 过渡形态：apply_patch 目前仍发 tool_use/apply_patch，不带路径
    # （backlog：改按路径发 file_write 后此别名并入 FileWrite）
    "ApplyPatch": {"scope": "tool_use"},
}
