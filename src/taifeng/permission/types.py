"""Permission gate 类型。

提供 PermissionRequest / PermissionDecision / PermissionPolicy / PermissionRule
+ 内置 CliPrompter / CallbackPrompter。

设计要点（参考 docs/architecture/capabilities/permission-gate.md
    - PermissionRequest 含 typed 上下文字段（thread_id / submission_id / entry_skill_id /
      call_chain / turn_index）+ 开放 metadata（业务自定义不透明扩展点）；提供
      for_tool_call / for_script_exec / for_skill_dispatch 三个工厂方法强制 schema。
    - PermissionPolicy 支持 prompter_timeout_seconds（>0 时用 anyio.fail_after 包裹
      prompter；超时自动 deny + emit telemetry）。
    - call_chain 长度上限 32（更深视作异常，截断到末尾 32 个）。
"""

from __future__ import annotations

import fnmatch
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import anyio

from taifeng.suspend.signal import SuspendSignal

logger = logging.getLogger(__name__)


PermissionMode = Literal["allow", "deny", "ask"]


# G5d：粗粒度能力阶梯（ReadOnly < WorkspaceWrite < DangerFullAccess）。
# 仅作便利起点，展开为既有 scope 规则；不引入新机制（参照 claw-code permissions.rs）。
CapabilityTier = Literal["read_only", "workspace_write", "danger_full_access"]


PermissionScope = Literal[
    "tool_use",        # 调用某个工具
    "file_read",       # 读文件
    "file_write",      # 写文件
    "shell_exec",      # 执行 shell
    "network",         # 发起网络请求
    "compaction",      # 触发压缩
    "skill_dispatch",  # 派发到子 skill
    "script_exec",     # 执行脚本（含 Python / Bash 等业务脚本）
    "custom",
]


# 单个 PermissionRequest.call_chain 上限层数 —— 超出截断到末尾 N 项。
# 默认 max_call_depth=6，正常永不触发；上限只是边界保护，防止 telemetry 体积爆炸。
CALL_CHAIN_MAX_DEPTH = 32


@dataclass(frozen=True)
class PermissionRequest:
    """业务层收到的审批请求。

    必填位置字段（向后兼容）：``scope`` / ``target``。

    typed 上下文字段（全部 keyword-only，默认值兼容旧调用方）：
        - ``thread_id``: 所在会话/线程标识；业务用于审计与 UI 渲染
        - ``submission_id``: 当前 Submission 标识；同 thread 内多 turn 可区分
        - ``entry_skill_id``: 整个调用图的入口 skill
        - ``call_chain``: skill 调用栈，最深的在末尾；超过 32 层时截断
        - ``turn_index``: 父 turn 的迭代序号（从 1 起）

    开放扩展：``metadata: dict`` 由业务自定义（不透明；taifeng 不解析 keys）。
    业务侧若需透传任何领域上下文（自定义路由键等），全部塞进 ``metadata``——
    引擎只负责原样携带到 prompter / telemetry，不引入任何业务命名字段（R1）。
    """

    scope: PermissionScope
    """权限作用域。"""

    target: str
    """被审批的目标 —— 工具名 / 文件路径 / shell 命令 / skill_id 等。"""

    reason: str = ""
    """为何需要这个权限（LLM 给出的理由 / 业务侧填充）。"""

    metadata: dict[str, Any] = field(default_factory=dict)
    """附加上下文：业务自定义字段。

    重要：本字段是开放扩展点；taifeng 内部 handler 已切到 typed 字段构造，
    业务侧仅在自定义场景需要继续使用 metadata。
    """

    # ----- typed 上下文字段（keyword-only via dataclass kw_only-like 默认）-----
    thread_id: str = ""
    """所在 thread；空字符串表示未提供。"""

    submission_id: str = ""
    """当前 Submission；空字符串表示未提供。"""

    entry_skill_id: str = ""
    """入口 skill；空字符串表示未提供。"""

    call_chain: tuple[str, ...] = ()
    """skill 调用栈；最深的在末尾。超过 CALL_CHAIN_MAX_DEPTH 时截断到末尾。"""

    turn_index: int = 0
    """父 turn 的迭代序号（从 1 起；0 表示未提供）。"""

    def __post_init__(self) -> None:
        # call_chain 长度超过上限时截断到末尾 N 项（保留最近调用栈）
        if len(self.call_chain) > CALL_CHAIN_MAX_DEPTH:
            # frozen dataclass 内部赋值需用 object.__setattr__
            object.__setattr__(
                self,
                "call_chain",
                tuple(self.call_chain[-CALL_CHAIN_MAX_DEPTH:]),
            )

    # ------------------------------------------------------------------
    # 工厂方法 —— required keyword-only 参数强制业务侧传上下文
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
        """构造"工具调用"审批请求。

        Args:
            tool_name: 工具名（如 ``shell_exec``）
            args: 工具参数（仅前 200 字符序列化进 metadata）
            thread_id / submission_id / entry_skill_id / turn_index: 上下文必填
            call_chain: 当前 skill 调用栈（默认空 = 在 entry skill 上）
            extra_metadata: 业务侧不透明上下文，原样合并进 ``metadata``（taifeng 不解析）
            reason: 可选审批原因（LLM 给的理由）

        Returns:
            PermissionRequest with scope='tool_use'
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
        """构造"脚本执行"审批请求（含 Bash / Python 脚本 / 业务自定义脚本）。

        ``extra_metadata`` 业务侧不透明上下文，原样合并进 ``metadata``（taifeng 不解析）。

        Returns:
            PermissionRequest with scope='script_exec'
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
        """构造"子 skill 派发"审批请求。

        Args:
            target_skill_id: 即将被派发执行的 child skill
            caller_skill_id: 当前正在执行的 skill（call_chain 的最后一个）
            call_chain: 当前 skill 调用栈
            extra_metadata: 业务侧不透明上下文，原样合并进 ``metadata``（taifeng 不解析）
            其余字段：上下文必填

        Returns:
            PermissionRequest with scope='skill_dispatch'
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
    """审批结果。"""

    granted: bool
    mode: PermissionMode
    reason: str = ""
    remember_until: Literal["once", "session", "always"] | None = None
    """决策记忆窗口 —— ``once``=本次；``session``=本 session；``always``=永久记入策略。"""

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


@runtime_checkable
class PermissionPrompter(Protocol):
    """业务层提供的 ``ask`` 模式实现。

    Engine 调 ``prompt`` 后阻塞等待用户响应。业务层负责：
        - CLI 风格：terminal y/n 提示
        - Web SSE 风格：把 request 推给前端 + 用 future 等回调
        - Slack/Discord 风格：发消息 + bot 等回复
    """

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        ...


# === Telemetry hook ===


PolicyTelemetryCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
"""PermissionPolicy 用于发 telemetry 事件的回调。

签名：``async def cb(event_kind: str, payload: dict) -> None``
事件 kind：``permission_prompt_timeout``。
"""


# === Policy ===


def _match_pattern(pattern: str, value: str) -> bool:
    """三态模式匹配 —— 字面 / ``re:`` 正则 / ``glob:`` 通配。

    - ``"foo"`` → 严格 equal
    - ``"re:^foo.*"`` → ``re.search`` 匹配
    - ``"glob:foo*"`` → ``fnmatch.fnmatch`` 匹配（``*`` / ``?`` / ``[seq]``）

    无效正则 → 返回 False + warning log（保守不命中，避免崩主流程）。
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
    """匹配规则 —— scope + target pattern + 可选 args_match + 决策 mode。

    pattern 语义见 ``_match_pattern``：字面 / ``re:`` / ``glob:`` 三态。
    ``target_pattern`` 与 ``args_match`` 的 value 都遵循同一套语法。
    """

    scope: PermissionScope
    target_pattern: str
    """字面 / ``re:`` 正则 / ``glob:`` 通配 匹配 ``request.target``。"""

    mode: PermissionMode
    reason: str = ""

    args_match: dict[str, str] | None = None
    """可选：args 级精确匹配。key=request.metadata['args'] 的字段名，value=pattern。
    AND 语义：所有 key 都要匹配；任一 args 缺 key → 不命中（保守）。
    None（默认）→ 不做 args 检查（向后兼容既有规则）。"""

    def matches(self, request: PermissionRequest) -> bool:
        if request.scope != self.scope:
            return False
        if not _match_pattern(self.target_pattern, request.target):
            return False
        # args_match：仅 metadata 含 args 字典时才能命中
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
        """解析 Claude Code 风格的规则字符串为 PermissionRule。

        支持语法：``<Alias>(<payload>)``，alias 见 ``_PERMISSION_ALIAS_TABLE``。

        payload 解释：
            - alias 含 args_key 时（Bash/FileRead/FileWrite/ShellExec）：
              payload 是 args[key] 的 pattern；target 固定（如 "shell_exec"）
            - alias 不含 args_key 时（Skill/Script/ApplyPatch）：
              payload 是 target_pattern；args_match 留 None
            - payload 为空 / ``"*"`` → 全匹配（target 走 ``"glob:*"`` /
              args_match 走 ``{key: "glob:*"}``）
            - 含 ``*`` / ``?`` / ``[`` → 自动加 ``glob:`` 前缀（除非已带 ``re:``）

        Raises:
            ValueError: 语法错误 / 未知 alias / 空字符串
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
            # tool_use scope + 固定 target + args_match
            assert fixed_target is not None
            return cls(
                scope=scope,
                target_pattern=fixed_target,
                mode=mode,
                reason=reason or f"parsed:{rule_str}",
                args_match={args_key: normalized},
            )
        # 无 args_key：payload 是 target_pattern
        return cls(
            scope=scope,
            target_pattern=normalized,
            mode=mode,
            reason=reason or f"parsed:{rule_str}",
        )


# ``<Alias>(<payload>)`` 语法解析正则
_PERMISSION_RULE_SYNTAX_RE = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)


def _normalize_pattern(raw: str) -> str:
    """归一化 parse 路径下的用户输入 pattern：

    - 空串 / "*" → ``"glob:*"`` （全匹配）
    - 已带 ``re:`` 或 ``glob:`` 前缀 → 原样
    - 含 glob 通配字符（``*`` / ``?`` / ``[``）→ 自动加 ``glob:`` 前缀
    - 其他 → 字面（不加前缀）
    """
    if raw == "" or raw == "*":
        return "glob:*"
    if raw.startswith("re:") or raw.startswith("glob:"):
        return raw
    if any(ch in raw for ch in ("*", "?", "[")):
        return f"glob:{raw}"
    return raw


# Claude Code 风格语法的 alias 表 —— 每条声明 scope + 可选固定 target +
# 可选 args 字段名。新增 alias 在这里加一条即可。
_PERMISSION_ALIAS_TABLE: dict[str, dict[str, Any]] = {
    # shell 执行 —— payload 是 cmd
    "Bash":       {"scope": "tool_use", "target": "shell_exec", "args_key": "cmd"},
    "ShellExec":  {"scope": "tool_use", "target": "shell_exec", "args_key": "cmd"},
    # 子 skill 派发 —— payload 是 target_pattern
    "Skill":      {"scope": "skill_dispatch"},
    # 脚本执行（SKILL.md scripts）—— payload 是 script_name pattern
    "Script":     {"scope": "script_exec"},
    # 文件 IO —— payload 是 path
    "FileRead":   {"scope": "tool_use", "target": "file_read",  "args_key": "path"},
    "FileWrite":  {"scope": "tool_use", "target": "file_write", "args_key": "path"},
    # 结构化补丁 —— payload 是 root_dir 之类的 path（暂无 args_key，按 target 匹配）
    "ApplyPatch": {"scope": "tool_use", "target": "apply_patch"},
}


@dataclass
class PermissionPolicy:
    """完整权限策略 = 规则列表 + 默认 mode + 可选 prompter + prompter timeout。

    检查顺序：
        1. 遍历 rules，首个 matches 返回该 mode
        2. 全不命中 → default_mode
        3. 若 mode == ``ask`` 且 prompter 非空 → 调 prompter（用 prompter_timeout_seconds 包裹）
        4. ``ask`` 但无 prompter → 默认 deny（保守）
    """

    rules: list[PermissionRule] = field(default_factory=list)
    default_mode: PermissionMode = "ask"
    prompter: PermissionPrompter | None = None

    prompter_timeout_seconds: float = 0
    """prompter 超时秒数。0 = 不超时（默认；保持原行为）；>0 用 anyio.fail_after 包裹。"""

    telemetry: PolicyTelemetryCallback | None = None
    """业务侧注入的 telemetry 回调；用于发 ``permission_prompt_timeout`` 事件。"""

    # 一次性预批准的 call_id 集合（resume 时 engine 注入，check 命中即放行并消费）。
    # 用 field(default_factory=set) 持有可变状态；不参与 from_dict / from_capability_tier
    # 构造（它们走 cls(rules=..., default_mode=..., prompter=...) 不传本字段，default_factory
    # 兜底为空集，行为零变化）。
    _preapproved_call_ids: set[str] = field(default_factory=set)

    def preapprove(self, call_id: str) -> None:
        """登记一次性预批准：下一次该 call_id 的 check 直接放行（不 prompt）。

        用于 resume：人类已通过 Resume 决定批准某挂起 tool call，engine 重跑该 call
        前调用本方法，避免再次触发 prompter（否则 SuspendingPrompter 会无限挂起）。

        Args:
            call_id: 被批准的挂起 tool call id（对应 PermissionRequest.metadata['call_id']）。
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
        """从粗粒度能力阶梯构造策略（G5d）。

        阶梯只是常见场景的便利起点，展开为既有 scope 规则（与 per-builtin 检查
        一致），业务侧可在返回的 ``rules`` 上继续追加精细规则覆盖：

        - ``read_only``：仅允许读（``file_read``）；写/执行/网络一律拒绝；其余 deny。
        - ``workspace_write``：允许读 + 写（``file_read`` / ``file_write``）；
          其余（shell / network 等）落 default_mode=``ask``（无 prompter → 保守 deny）。
        - ``danger_full_access``：default_mode=``allow``，无限制（高信任 / dev）。

        Raises:
            ValueError: 未知 tier。
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
        """从 dict / JSON 配置构造 PermissionPolicy。

        支持两种自动识别的结构（**禁止混用**）：

        **Style A —— Claude Code 风格语法糖**::

            {
                "default_mode": "ask",      # 可选，缺省 "ask"
                "allow": ["Bash(openspec --help)", "Skill(read_*)"],
                "deny":  ["Bash(rm -rf *)"],
                "ask":   ["Bash(*)"],
            }

        **Style B —— 明文规则列表**::

            {
                "default_mode": "allow",
                "rules": [
                    {"scope": "tool_use", "target_pattern": "shell_exec",
                     "args_match": {"cmd": "re:^openspec\\\\s"},
                     "mode": "allow", "reason": "ops_safe"},
                ],
            }

        Style A 中 deny → allow → ask 顺序展开为 rules（deny 在前确保 short-circuit）。

        Raises:
            ValueError: 两种 style 混用、未知 alias、语法错、字段缺失
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
            # deny 优先：放在 rules 列表最前，命中后 short-circuit
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
        # 0. 一次性预批准：命中即消费 + 放行（resume 已获人类批准，勿再 prompt）。
        #    必须在规则/prompter 之前，否则 SuspendingPrompter 会再次挂起导致死循环。
        call_id = request.metadata.get("call_id")
        if call_id is not None and call_id in self._preapproved_call_ids:
            self._preapproved_call_ids.discard(call_id)
            return PermissionDecision.allow(reason="resume_preapproved")

        # 1+2. 规则查找
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
        if self.prompter is None:
            return PermissionDecision.deny(
                reason="ask_mode_no_prompter (默认保守拒绝)",
            )
        try:
            decision = await self._invoke_prompter(request)
        except TimeoutError:
            # 超时分支：发 telemetry + 返回 deny
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
            # 挂起信号不是错误:必须穿透 ask 模式的宽 except,交由 dispatch_batch 捕获挂起
            raise
        except Exception as e:
            logger.exception("prompter raised")
            return PermissionDecision.deny(reason=f"prompter_error: {e}")

        # permission-rule-args-match: Taifeng 内核不再因 remember="always"
        # 自动写回 rules（R1 业务零侵入：内核无 session 权限状态）。
        # 业务侧若想 session/持久层记忆，在 prompter 包装层读 decision.remember_until
        # 后自管：
        #     async def wrap(req):
        #         d = await inner.prompt(req)
        #         if d.remember_until == "always":
        #             business_store.persist(req, d)
        #         return d
        return decision

    async def _invoke_prompter(self, request: PermissionRequest) -> PermissionDecision:
        """调用 prompter；按 prompter_timeout_seconds 决定是否包裹 fail_after。

        Raises:
            TimeoutError: 超时（仅 prompter_timeout_seconds > 0 时可能抛出）
        """
        assert self.prompter is not None  # check() 入口已保证
        if self.prompter_timeout_seconds and self.prompter_timeout_seconds > 0:
            # anyio.fail_after：超时抛 TimeoutError；同时取消 prompter 内 await
            with anyio.fail_after(self.prompter_timeout_seconds):
                return await self.prompter.prompt(request)
        # 不超时路径：性能不退化，与原行为一致
        return await self.prompter.prompt(request)

    async def _emit_telemetry(self, kind: str, payload: dict[str, Any]) -> None:
        """安全 emit telemetry —— 任何异常被吞掉（不影响主流程）。"""
        if self.telemetry is None:
            return
        try:
            await self.telemetry(kind, payload)
        except Exception:
            logger.exception("permission telemetry callback raised (ignored)")


# === 内置 prompter 实现 ===


class CliPrompter(PermissionPrompter):
    """CLI 风格 prompter —— stdin/stdout 询问 y/n。

    适用本地开发 / pytest 中 monkeypatch 替换。

    注：``always-allow`` / ``always-deny`` 选项仍返回 ``remember="always"`` 的
    PermissionDecision，但自 ``permission-rule-args-match`` 起 Taifeng 内核
    **不再**自动把该决策持久化到 policy.rules（R1 业务零侵入）。业务侧若想
    实现 "用户点一次始终允许、下次不再问" 的语义，在自己的 prompter 包装层
    读 ``decision.remember_until`` 后自管 policy.rules.append 或落 DB。
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


PromptCallback = Callable[[PermissionRequest], Awaitable[PermissionDecision]]


class CallbackPrompter(PermissionPrompter):
    """异步回调风格 prompter —— Web/Slack 等业务层包装。"""

    def __init__(self, callback: PromptCallback) -> None:
        self._callback = callback

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        return await self._callback(request)


class SuspendingPrompter(PermissionPrompter):
    """挂起式 prompter —— ask 模式不阻塞,抛 SuspendSignal 让 turn 退栈挂起。

    参照 openclaw「tool 早返回 approval-pending」:差异是 taifeng 不返回状态码,
    而是抛控制流异常,由 run_turn 聚合为 TurnSuspended。

    request_id 由注入工厂生成(测试可固定);payload_schema 给出审批决定的 JSON Schema。
    """

    # 审批决定的 JSON Schema —— 业务/前端据此渲染审批 UI
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
        """初始化 SuspendingPrompter。

        Args:
            request_id_factory: 注入式 id 工厂(测试可固定);默认随机生成。
        """
        # 注入式 id 工厂(测试可固定);默认随机
        self._gen_id = request_id_factory or (lambda: f"req_{secrets.token_hex(6)}")

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        """不返回 —— 总是抛 SuspendSignal(reason=PERMISSION)。

        Raises:
            SuspendSignal: 总是抛出,使 turn 退栈挂起等待审批。
        """
        # 延迟导入避免循环依赖（suspend 包不依赖 permission 包）
        from taifeng.suspend.reason import PendingRequest, SuspendReason
        from taifeng.suspend.signal import SuspendSignal

        # detail:把审批上下文透传给前端(R1:taifeng 不解析,仅携带)
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
