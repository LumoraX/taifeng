"""G5d：权限能力阶梯 PermissionPolicy.from_capability_tier。

阶梯是「粗粒度起点」便利构造，展开为既有 scope 规则（与 per-builtin 检查一致），
不引入新机制 —— ReadOnly < WorkspaceWrite < DangerFullAccess。
"""

from __future__ import annotations

import pytest

from taifeng.permission.types import PermissionPolicy, PermissionRequest


async def _decide(policy: PermissionPolicy, scope: str, target: str) -> str:
    req = PermissionRequest(scope=scope, target=target)  # type: ignore[arg-type]
    d = await policy.check(req)
    return d.mode


@pytest.mark.asyncio
async def test_read_only_allows_read_denies_write_shell_network() -> None:
    p = PermissionPolicy.from_capability_tier("read_only")
    assert await _decide(p, "file_read", "/x") == "allow"
    assert await _decide(p, "file_write", "/x") == "deny"
    assert await _decide(p, "shell_exec", "ls") == "deny"
    assert await _decide(p, "network", "http://x") == "deny"


@pytest.mark.asyncio
async def test_workspace_write_allows_rw_asks_unknown_denies_nothing_silently() -> None:
    p = PermissionPolicy.from_capability_tier("workspace_write")
    assert await _decide(p, "file_read", "/x") == "allow"
    assert await _decide(p, "file_write", "/x") == "allow"
    # 未显式列出的（如 shell/network）→ default_mode=ask（无 prompter → deny）
    assert await _decide(p, "shell_exec", "rm") == "deny"  # ask 无 prompter → 保守 deny


@pytest.mark.asyncio
async def test_danger_full_access_allows_everything() -> None:
    p = PermissionPolicy.from_capability_tier("danger_full_access")
    assert await _decide(p, "shell_exec", "rm -rf /") == "allow"
    assert await _decide(p, "network", "http://x") == "allow"
    assert await _decide(p, "file_write", "/x") == "allow"


def test_invalid_tier_raises() -> None:
    with pytest.raises(ValueError):
        PermissionPolicy.from_capability_tier("god_mode")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tier_accepts_prompter_passthrough() -> None:
    seen = []

    class _P:
        async def prompt(self, request):  # noqa: ANN001
            seen.append(request.scope)
            from taifeng.permission.types import PermissionDecision
            return PermissionDecision.allow(reason="user")

    p = PermissionPolicy.from_capability_tier("workspace_write", prompter=_P())
    # shell_exec 未列出 → ask → 调 prompter
    assert await _decide(p, "shell_exec", "ls") == "allow"
    assert seen == ["shell_exec"]
