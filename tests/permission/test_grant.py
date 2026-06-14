"""permission-grants: PermissionGrant + GrantStore 单元覆盖。

grant = 「预先答好的 ask」：命中即放行、绕过 prompter，但绝不越过 deny 规则
（deny 由 policy.check 在 grant 之前裁决，本文件只测 store 层的匹配/生命周期）。

覆盖：
    - 匹配复用 PermissionRule 三态（literal / re: / glob:）+ args_match
    - call_chain_prefix 子树收窄（子命中、父/兄弟 miss）
    - thread_id 绑定（空=任意、非空=精确）
    - max_uses 确定性生命周期（递减 + 到 0 失效移除）
    - revoke 主动撤销
    - 无匹配返回 None
    - add 自动分配确定性 grant_id（计数器，resume 友好）
    - snapshot 反映剩余次数（resume 重种用）
"""

from __future__ import annotations

from taifeng.permission import PermissionGrant, PermissionRequest
from taifeng.permission.grant import GrantStore


def _req(
    cmd: str,
    *,
    target: str = "shell_exec",
    call_chain: tuple[str, ...] = (),
    thread_id: str = "t",
) -> PermissionRequest:
    """构造一个 tool_use 审批请求（默认 shell_exec + args.cmd）。"""
    return PermissionRequest.for_tool_call(
        target,
        {"cmd": cmd},
        thread_id=thread_id,
        submission_id="s",
        entry_skill_id="e",
        turn_index=1,
        call_chain=call_chain,
    )


# ==============================================================
# 1. 匹配：复用 PermissionRule 三态 + args_match
# ==============================================================


def test_match_literal_target() -> None:
    store = GrantStore()
    store.add(PermissionGrant(scope="tool_use", target_pattern="shell_exec"))
    assert store.match(_req("ls")) is not None


def test_match_glob_args() -> None:
    store = GrantStore()
    store.add(
        PermissionGrant(
            scope="tool_use",
            target_pattern="shell_exec",
            args_match={"cmd": "glob:openspec *"},
        )
    )
    assert store.match(_req("openspec list")) is not None
    # args 不匹配 → miss
    assert store.match(_req("rm -rf /")) is None


def test_match_regex_args() -> None:
    store = GrantStore()
    store.add(
        PermissionGrant(
            scope="tool_use",
            target_pattern="shell_exec",
            args_match={"cmd": r"re:^git\s"},
        )
    )
    assert store.match(_req("git status")) is not None
    assert store.match(_req("gitx")) is None


def test_scope_mismatch_misses() -> None:
    store = GrantStore()
    store.add(PermissionGrant(scope="skill_dispatch", target_pattern="glob:*"))
    assert store.match(_req("ls")) is None  # 请求是 tool_use


# ==============================================================
# 2. call_chain_prefix 子树收窄
# ==============================================================


def test_prefix_empty_matches_tree_wide() -> None:
    store = GrantStore()
    store.add(PermissionGrant(scope="tool_use", target_pattern="shell_exec"))
    # 空前缀 = 全树：任意 call_chain 都命中
    assert store.match(_req("ls", call_chain=())) is not None
    assert store.match(_req("ls", call_chain=("root", "child"))) is not None


def test_prefix_narrows_to_subtree() -> None:
    store = GrantStore()
    store.add(
        PermissionGrant(
            scope="tool_use",
            target_pattern="shell_exec",
            call_chain_prefix=("root", "expert"),
        )
    )
    # 子树内（前缀匹配）→ 命中
    assert store.match(_req("ls", call_chain=("root", "expert", "leaf"))) is not None
    assert store.match(_req("ls", call_chain=("root", "expert"))) is not None
    # 父（更短，非该前缀）→ miss
    assert store.match(_req("ls", call_chain=("root",))) is None
    # 兄弟子树 → miss
    assert store.match(_req("ls", call_chain=("root", "other"))) is None


# ==============================================================
# 3. thread_id 绑定
# ==============================================================


def test_thread_binding() -> None:
    store = GrantStore()
    store.add(
        PermissionGrant(
            scope="tool_use", target_pattern="shell_exec", thread_id="thread-A"
        )
    )
    assert store.match(_req("ls", thread_id="thread-A")) is not None
    assert store.match(_req("ls", thread_id="thread-B")) is None


def test_thread_empty_matches_any() -> None:
    store = GrantStore()
    store.add(PermissionGrant(scope="tool_use", target_pattern="shell_exec"))
    assert store.match(_req("ls", thread_id="whatever")) is not None


# ==============================================================
# 4. max_uses 确定性生命周期
# ==============================================================


def test_max_uses_decrement_and_expire() -> None:
    store = GrantStore()
    g = store.add(
        PermissionGrant(scope="tool_use", target_pattern="shell_exec", max_uses=2)
    )
    # 第 1 次：命中 + consume → 未失效
    assert store.match(_req("ls")) is not None
    assert store.consume(g) is False
    # 第 2 次：命中 + consume → 失效（remaining 到 0，移除）
    assert store.match(_req("ls")) is not None
    assert store.consume(g) is True
    # 第 3 次：已移除 → miss
    assert store.match(_req("ls")) is None


def test_unlimited_never_expires() -> None:
    store = GrantStore()
    g = store.add(
        PermissionGrant(scope="tool_use", target_pattern="shell_exec", max_uses=None)
    )
    for _ in range(5):
        assert store.match(_req("ls")) is not None
        assert store.consume(g) is False
    assert store.match(_req("ls")) is not None


# ==============================================================
# 5. revoke / 无匹配 / grant_id / snapshot
# ==============================================================


def test_revoke_removes() -> None:
    store = GrantStore()
    g = store.add(PermissionGrant(scope="tool_use", target_pattern="shell_exec"))
    assert store.revoke(g.grant_id) is True
    assert store.match(_req("ls")) is None
    # 重复 revoke → False
    assert store.revoke(g.grant_id) is False


def test_no_match_returns_none() -> None:
    store = GrantStore()  # 空 store
    assert store.match(_req("ls")) is None


def test_add_assigns_deterministic_grant_id() -> None:
    store = GrantStore()
    g0 = store.add(PermissionGrant(scope="tool_use", target_pattern="a"))
    g1 = store.add(PermissionGrant(scope="tool_use", target_pattern="b"))
    # 自动分配、确定性、互不相同（resume 友好：计数器而非随机）
    assert g0.grant_id and g1.grant_id and g0.grant_id != g1.grant_id


def test_add_keeps_explicit_grant_id() -> None:
    store = GrantStore()
    g = store.add(
        PermissionGrant(
            scope="tool_use", target_pattern="a", grant_id="biz-supplied"
        )
    )
    assert g.grant_id == "biz-supplied"


def test_snapshot_reflects_remaining_uses() -> None:
    store = GrantStore()
    g = store.add(
        PermissionGrant(scope="tool_use", target_pattern="shell_exec", max_uses=3)
    )
    store.consume(g)  # 用掉 1，剩 2
    snap = store.snapshot()
    assert len(snap) == 1
    # snapshot 把剩余次数反映为 max_uses，便于 resume 原样重种
    assert snap[0].max_uses == 2
