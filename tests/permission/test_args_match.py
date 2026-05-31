"""permission-rule-args-match: PermissionRule.matches 升级覆盖。

覆盖：
    - target_pattern 三态（literal / re: / glob:）
    - args_match 三态 + AND 语义 + args 缺 key
    - args_match=None 时向后兼容
    - scope mismatch 立即 False
"""

from __future__ import annotations

import pytest

from taifeng.permission import (
    PermissionRequest,
    PermissionRule,
)


def _req_tool(cmd: str, *, target: str = "shell_exec") -> PermissionRequest:
    return PermissionRequest.for_tool_call(
        target,
        {"cmd": cmd},
        thread_id="t",
        submission_id="s",
        entry_skill_id="e",
        turn_index=1,
    )


# ==============================================================
# 1. scope mismatch
# ==============================================================


def test_scope_mismatch_does_not_match() -> None:
    rule = PermissionRule(
        scope="skill_dispatch", target_pattern="x", mode="allow",
    )
    req = _req_tool("ls")
    assert rule.matches(req) is False


# ==============================================================
# 2. target_pattern 三态
# ==============================================================


def test_target_pattern_literal() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="allow",
    )
    assert rule.matches(_req_tool("anything")) is True
    assert rule.matches(_req_tool("anything", target="file_read")) is False


def test_target_pattern_regex() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="re:^(shell|bash)_exec$",
        mode="allow",
    )
    assert rule.matches(_req_tool("x")) is True


def test_target_pattern_glob() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="glob:shell_*", mode="allow",
    )
    assert rule.matches(_req_tool("x")) is True
    assert rule.matches(_req_tool("x", target="file_read")) is False


# ==============================================================
# 3. args_match 三态
# ==============================================================


def test_args_match_literal_hit() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="allow",
        args_match={"cmd": "openspec --help"},
    )
    assert rule.matches(_req_tool("openspec --help")) is True
    assert rule.matches(_req_tool("openspec status")) is False


def test_args_match_regex() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="deny",
        args_match={"cmd": "re:^rm\\s+-rf"},
    )
    assert rule.matches(_req_tool("rm -rf /tmp")) is True
    assert rule.matches(_req_tool("rm -i /tmp")) is False


def test_args_match_glob() -> None:
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="allow",
        args_match={"cmd": "glob:openspec *"},
    )
    assert rule.matches(_req_tool("openspec instructions x")) is True
    assert rule.matches(_req_tool("rm -rf /")) is False


# ==============================================================
# 4. args_match 缺 key / AND 语义
# ==============================================================


def test_args_match_missing_key_does_not_match() -> None:
    """rule 要求 cmd 字段但 args 里没有 → 不命中（保守）。"""
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="allow",
        args_match={"cmd": "ls"},
    )
    # 构造一个 args 不含 cmd 的 request（直接走构造器）
    req = PermissionRequest.for_tool_call(
        "shell_exec",
        {"foo": "bar"},  # 没有 cmd
        thread_id="t", submission_id="s", entry_skill_id="e", turn_index=1,
    )
    assert rule.matches(req) is False


def test_args_match_skill_dispatch_no_args_metadata() -> None:
    """skill_dispatch scope 的 metadata 不含 args dict → 带 args_match 不命中。"""
    rule = PermissionRule(
        scope="skill_dispatch", target_pattern="backend-reviewer", mode="allow",
        args_match={"some_key": "x"},
    )
    req = PermissionRequest.for_skill_dispatch(
        "backend-reviewer",
        caller_skill_id="general", call_chain=("general",),
        thread_id="t", submission_id="s", entry_skill_id="e", turn_index=1,
    )
    # skill_dispatch metadata 是 {"caller_skill_id": ...}，没有 "args"
    assert rule.matches(req) is False


def test_args_match_and_semantics_all_keys_must_match() -> None:
    """多 key AND：任一不匹配 → False。"""
    rule = PermissionRule(
        scope="tool_use", target_pattern="apply_patch", mode="allow",
        args_match={"root_dir": "/src", "max_bytes": "1000"},
    )
    req_all_match = PermissionRequest.for_tool_call(
        "apply_patch",
        {"root_dir": "/src", "max_bytes": "1000"},
        thread_id="t", submission_id="s", entry_skill_id="e", turn_index=1,
    )
    req_one_diff = PermissionRequest.for_tool_call(
        "apply_patch",
        {"root_dir": "/src", "max_bytes": "9999"},
        thread_id="t", submission_id="s", entry_skill_id="e", turn_index=1,
    )
    assert rule.matches(req_all_match) is True
    assert rule.matches(req_one_diff) is False


# ==============================================================
# 5. 向后兼容：不带 args_match 的规则
# ==============================================================


def test_rule_without_args_match_is_backwards_compatible() -> None:
    """未传 args_match (= None) → 行为完全等价旧 PermissionRule。"""
    rule = PermissionRule(
        scope="tool_use", target_pattern="shell_exec", mode="allow",
    )
    assert rule.args_match is None
    assert rule.matches(_req_tool("anything")) is True


# ==============================================================
# 6. 无效正则 fallback 不命中（safe）
# ==============================================================


def test_invalid_regex_safely_misses(caplog: pytest.LogCaptureFixture) -> None:
    """正则编译失败 → 返回 False + warning（不抛异常）。"""
    rule = PermissionRule(
        scope="tool_use", target_pattern="re:[", mode="allow",
    )
    assert rule.matches(_req_tool("anything")) is False
