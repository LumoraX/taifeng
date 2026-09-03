"""效果授权模型（ADR 0028）—— Style A 语法糖必须命中内置工具真实发出的请求。

内置工具按效果发请求：shell_exec / file_read / file_write / network，target 是规范化
后的作用对象（命令串 / 绝对路径 / "METHOD URL"）。Style A 别名（Bash / FileRead /
FileWrite / Network）解析后须与这些请求同 scope，否则 deny 规则永远不生效。
这里的用例走**真实工具 handler**，不直接调 policy.check。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import PermissionPolicy, PermissionRequest, PermissionRule
from taifeng.tool.builtins.file_io import make_file_read_tool
from taifeng.tool.builtins.shell import make_shell_exec_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _seed_files(root: Path) -> Path:
    """同步 helper 写 fixture 文件并返回 resolved root（规避 ASYNC240）。"""
    (root / "secret.txt").write_text("s3cr3t", encoding="utf-8")
    (root / "public.txt").write_text("hello", encoding="utf-8")
    return root.resolve()


def _ctx() -> ToolContext:
    return ToolContext(
        call_id="c-1", cancel=CancellationToken(), thread_id="t-1", extras={},
    )


# ---------------------------------------------------------------------------
# 端到端：Style A deny 对真实内置工具生效
# ---------------------------------------------------------------------------


async def test_bash_deny_rule_blocks_real_shell_exec() -> None:
    """`Bash(echo *)` deny → shell_exec(command="echo hi") 返回 permission_denied，不执行。"""
    policy = PermissionPolicy.from_dict(
        {"default_mode": "allow", "deny": ["Bash(echo *)"]},
    )
    tool = make_shell_exec_tool(policy=policy)
    result = await tool.handler({"command": "echo should-not-run"}, _ctx())
    assert result.is_error
    assert result.data["reason"] == "permission_denied"


async def test_file_read_glob_rule_matches_resolved_absolute_path(tmp_path: Path) -> None:
    """`FileRead(<root>/secret*)` deny → file_read(path="secret.txt") 被拒（target 是绝对路径）。"""
    resolved_root = _seed_files(tmp_path)
    policy = PermissionPolicy.from_dict(
        {"default_mode": "allow", "deny": [f"FileRead({resolved_root}/secret*)"]},
    )
    tool = make_file_read_tool(root_dir=tmp_path, policy=policy)

    denied = await tool.handler({"path": "secret.txt"}, _ctx())
    assert denied.is_error
    assert denied.data["reason"] == "permission_denied"

    allowed = await tool.handler({"path": "public.txt"}, _ctx())
    assert not allowed.is_error
    assert "hello" in allowed.output


# ---------------------------------------------------------------------------
# 解析形状：别名 → 效果 scope，不再产出 args_match
# ---------------------------------------------------------------------------


def test_parse_bash_yields_shell_exec_scope_without_args_match() -> None:
    rule = PermissionRule.parse("Bash(openspec *)", mode="allow")
    assert rule.scope == "shell_exec"
    assert rule.target_pattern == "glob:openspec *"
    assert rule.args_match is None


def test_parse_file_write_yields_file_write_scope() -> None:
    rule = PermissionRule.parse("FileWrite(/data/*)", mode="deny")
    assert rule.scope == "file_write"
    assert rule.target_pattern == "glob:/data/*"
    assert rule.args_match is None


def test_parse_network_without_method_matches_any_method() -> None:
    """省略 method → 前缀 `* ` 后归一，任意 method 命中。"""
    rule = PermissionRule.parse("Network(https://api.example.com/*)", mode="allow")
    assert rule.scope == "network"
    assert rule.target_pattern == "glob:* https://api.example.com/*"
    assert rule.matches(
        PermissionRequest(scope="network", target="GET https://api.example.com/v1"),
    )
    assert rule.matches(
        PermissionRequest(scope="network", target="POST https://api.example.com/v1"),
    )


def test_parse_network_with_method_is_method_specific() -> None:
    rule = PermissionRule.parse("Network(GET https://api.example.com/*)", mode="allow")
    assert rule.target_pattern == "glob:GET https://api.example.com/*"
    assert rule.matches(
        PermissionRequest(scope="network", target="GET https://api.example.com/v1"),
    )
    assert not rule.matches(
        PermissionRequest(scope="network", target="POST https://api.example.com/v1"),
    )


def test_parse_network_literal_url_without_wildcard() -> None:
    """字面 URL（无通配）也要能匹配任意 method：`* ` 前缀引入 glob。"""
    rule = PermissionRule.parse("Network(https://a.example/x)", mode="allow")
    assert rule.target_pattern == "glob:* https://a.example/x"
    assert rule.matches(PermissionRequest(scope="network", target="GET https://a.example/x"))


def test_parse_network_regex_passthrough() -> None:
    """re: 模式原样透传，作者自行处理 method 前缀。"""
    rule = PermissionRule.parse("Network(re:^GET https://a\\.example/)", mode="allow")
    assert rule.target_pattern == "re:^GET https://a\\.example/"
