"""permission-rule-args-match: PermissionRule.parse + PermissionPolicy.from_dict。

覆盖：
    - PermissionRule.parse 各 alias 语法
    - PermissionRule.parse 错误处理（未知 alias / 语法错）
    - PermissionPolicy.from_dict Style A（allow/deny/ask 列表）
    - PermissionPolicy.from_dict Style B（明文 rules）
    - 混用报错
    - e2e：from_dict 出来的 policy 实际 check 正确
"""

from __future__ import annotations

import pytest

from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)

# ==============================================================
# 1. PermissionRule.parse
# ==============================================================


def test_parse_bash_literal() -> None:
    """效果模型（ADR 0028）：Bash → scope=shell_exec，payload 即 target_pattern。"""
    rule = PermissionRule.parse("Bash(openspec --help)", mode="allow")
    assert rule.scope == "shell_exec"
    assert rule.target_pattern == "openspec --help"
    assert rule.mode == "allow"
    assert rule.args_match is None


def test_parse_bash_glob_auto_prefix() -> None:
    """payload 含 * → 自动加 glob: 前缀。"""
    rule = PermissionRule.parse("Bash(openspec *)", mode="allow")
    assert rule.target_pattern == "glob:openspec *"


def test_parse_bash_regex_prefix_kept() -> None:
    """payload 已带 re: → 原样保留。"""
    rule = PermissionRule.parse(
        "Bash(re:^rm\\s+-rf\\s+\\./data)", mode="allow",
    )
    assert rule.target_pattern == "re:^rm\\s+-rf\\s+\\./data"


def test_parse_bash_wildcard_only() -> None:
    """* / 空 → glob:* 全匹配。"""
    rule = PermissionRule.parse("Bash(*)", mode="ask")
    assert rule.target_pattern == "glob:*"
    rule2 = PermissionRule.parse("Bash()", mode="ask")
    assert rule2.target_pattern == "glob:*"


def test_parse_skill_with_glob() -> None:
    """Skill alias 没有 args_key → payload 是 target_pattern。"""
    rule = PermissionRule.parse("Skill(read_*)", mode="allow")
    assert rule.scope == "skill_dispatch"
    assert rule.target_pattern == "glob:read_*"
    assert rule.args_match is None


def test_parse_file_read() -> None:
    rule = PermissionRule.parse("FileRead(/data/*)", mode="allow")
    assert rule.scope == "file_read"
    assert rule.target_pattern == "glob:/data/*"
    assert rule.args_match is None


def test_parse_script_alias() -> None:
    rule = PermissionRule.parse("Script(apply_delta)", mode="allow")
    assert rule.scope == "script_exec"
    assert rule.target_pattern == "apply_delta"
    assert rule.args_match is None


def test_parse_apply_patch_alias() -> None:
    rule = PermissionRule.parse("ApplyPatch(*)", mode="ask")
    assert rule.scope == "tool_use"
    assert rule.target_pattern == "glob:*"


def test_parse_unknown_alias_raises() -> None:
    with pytest.raises(ValueError, match="unknown_permission_syntax"):
        PermissionRule.parse("Unknown(x)", mode="allow")


def test_parse_invalid_syntax_raises() -> None:
    """无括号 → invalid syntax。"""
    with pytest.raises(ValueError, match="invalid_permission_syntax"):
        PermissionRule.parse("BashLs", mode="allow")


# ==============================================================
# 2. PermissionPolicy.from_dict Style A
# ==============================================================


def test_from_dict_style_a_single_allow() -> None:
    policy = PermissionPolicy.from_dict(
        {"allow": ["Bash(openspec --help)"]},
    )
    assert len(policy.rules) == 1
    assert policy.rules[0].mode == "allow"
    assert policy.default_mode == "ask"  # 缺省


def test_from_dict_style_a_three_modes() -> None:
    policy = PermissionPolicy.from_dict({
        "default_mode": "deny",
        "allow": ["Bash(openspec *)"],
        "deny":  ["Bash(rm -rf *)"],
        "ask":   ["Bash(*)"],
    })
    assert policy.default_mode == "deny"
    # deny 优先排在最前
    modes = [r.mode for r in policy.rules]
    assert modes == ["deny", "allow", "ask"]


# ==============================================================
# 3. PermissionPolicy.from_dict Style B
# ==============================================================


def test_from_dict_style_b_rules_passthrough() -> None:
    policy = PermissionPolicy.from_dict({
        "default_mode": "allow",
        "rules": [
            {
                "scope": "tool_use",
                "target_pattern": "shell_exec",
                "args_match": {"cmd": "re:^openspec\\s"},
                "mode": "allow",
                "reason": "ops_safe",
            },
        ],
    })
    assert len(policy.rules) == 1
    r = policy.rules[0]
    assert r.scope == "tool_use"
    assert r.target_pattern == "shell_exec"
    assert r.args_match == {"cmd": "re:^openspec\\s"}
    assert r.mode == "allow"
    assert r.reason == "ops_safe"


def test_from_dict_invalid_rule_object_raises() -> None:
    with pytest.raises(ValueError, match="invalid_rule_object"):
        PermissionPolicy.from_dict({"rules": ["not a dict"]})


# ==============================================================
# 4. Style 混用报错
# ==============================================================


def test_from_dict_mix_style_raises() -> None:
    with pytest.raises(ValueError, match="permission_config_conflict"):
        PermissionPolicy.from_dict({
            "allow": ["Bash(x)"],
            "rules": [{"scope": "tool_use", "target_pattern": "x", "mode": "allow"}],
        })


def test_from_dict_invalid_default_mode_raises() -> None:
    with pytest.raises(ValueError, match="invalid_default_mode"):
        PermissionPolicy.from_dict({"default_mode": "maybe"})


# ==============================================================
# 5. e2e: from_dict → policy.check 真实决策
# ==============================================================


@pytest.mark.asyncio
async def test_from_dict_e2e_deny_overrides_allow() -> None:
    """deny + allow 同 dict 下，rm -rf 仍被 deny（命中 deny rule short-circuit）。"""
    policy = PermissionPolicy.from_dict({
        "default_mode": "ask",
        "allow": ["Bash(rm *)"],
        "deny":  ["Bash(re:^rm\\s+-rf)"],
    })

    # 内置 shell_exec 真实发出的效果形状：scope=shell_exec，target=完整命令串
    req_safe = PermissionRequest(scope="shell_exec", target="rm /tmp/x")
    req_dangerous = PermissionRequest(scope="shell_exec", target="rm -rf /")
    d_safe = await policy.check(req_safe)
    d_dangerous = await policy.check(req_dangerous)
    assert d_safe.granted is True       # 命中 allow Bash(rm *)
    assert d_dangerous.granted is False  # 命中 deny re 拦截


@pytest.mark.asyncio
async def test_from_dict_e2e_default_allow_with_specific_ask() -> None:
    """default_mode=allow + ask 列表 → 列表外全过，列表内走 prompter。"""
    prompter_called = 0

    async def cb(req: PermissionRequest) -> PermissionDecision:
        nonlocal prompter_called
        prompter_called += 1
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy.from_dict(
        {
            "default_mode": "allow",
            "ask": ["Bash(rm *)"],
        },
        prompter=CallbackPrompter(cb),
    )

    req_safe = PermissionRequest(scope="shell_exec", target="ls")
    req_ask = PermissionRequest(scope="shell_exec", target="rm /tmp/x")

    # req_safe (cmd=ls) → 不命中 Bash(rm *) → fallback default_mode=allow
    d_safe = await policy.check(req_safe)
    assert d_safe.granted is True
    assert prompter_called == 0    # default allow 命中 → 不走 prompter
    # req_ask (cmd=rm /tmp/x) → 命中 ask 规则 → 走 prompter
    d_ask = await policy.check(req_ask)
    assert d_ask.granted is True
    assert prompter_called == 1    # ask 规则命中 → 走 prompter
