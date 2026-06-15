"""GrantStore -- the in-memory store of reusable approval grants.

This is the kernel-side mechanism behind ``PermissionGrant``: an ordered list of
active grants plus their remaining-use counters, with matching delegated to the
existing ``PermissionRule.matches`` (so the literal / ``re:`` / ``glob:`` grammar is
never reimplemented).

参照 claw-code permissions (scoped approvals delegated across sub-tasks)；差异：
taifeng 不持挂钟 TTL（``src/`` 禁 ``Date.now`` 以保 resume 确定性），生命周期只用
确定性的 ``max_uses`` 计数，挂钟过期留 userspace 经 ``revoke`` 处理。

The store lives on a single engine-level ``PermissionPolicy`` instance, so a grant is
tree-wide by default (every root / call_skill / spawn_skill runner shares the same
policy); ``call_chain_prefix`` narrows a grant to a subtree.

Depends only on ``models`` (PermissionGrant / PermissionRequest) and ``rule``
(PermissionRule for matching reuse).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from taifeng.permission.rule import PermissionRule

if TYPE_CHECKING:
    # Type-only: these names appear solely in annotations (strings under
    # ``from __future__ import annotations``), so they stay out of the runtime imports.
    # PermissionGrant instances are still handled at runtime via dataclasses.replace,
    # but the *class symbol* is only referenced in annotations.
    from collections.abc import Callable

    from taifeng.permission.models import PermissionGrant, PermissionRequest


@dataclass
class _GrantEntry:
    """A stored grant plus its mutable remaining-use counter.

    The grant itself is frozen, so the decrementing counter cannot live on it; the
    store holds it here. ``remaining is None`` means an unbounded grant.
    """

    grant: PermissionGrant
    remaining: int | None


@dataclass
class GrantStore:
    """An ordered store of active reusable grants.

    Matching reuses ``PermissionRule.matches`` for the scope/target/args part, then
    layers on ``call_chain_prefix`` (subtree narrowing) and ``thread_id`` (thread
    binding). The first matching grant wins (insertion order).
    """

    _entries: list[_GrantEntry] = field(default_factory=list)
    _counter: int = 0
    id_factory: Callable[[], str] | None = None
    """Optional grant-id factory (injectable in tests). When None, ``add`` assigns a
    deterministic counter id (``grant-0`` / ``grant-1`` / ...), which keeps resume
    reproducible (no randomness)."""

    def add(self, grant: PermissionGrant) -> PermissionGrant:
        """Register a grant; dedup by signature + guarantee a unique grant_id.

        Two-step contract (修复 #3 id 撞车 + #4 无界堆积):
            1. **签名 dedup**：若已有活跃 grant 的匹配签名等价（scope/target/args/
               prefix/thread 全等，忽略 id/reason/max_uses）→ 直接复用既有那张、不新增
               （issue_grant 因此幂等；mint 重复不堆积）。
            2. **id 唯一**：空 id → 自动分配并**跳过已占用 id**（修复 resume 重种后
               ``_counter`` 不前移导致的撞车）；显式 id 已被占用 → ``raise ValueError``
               （禁 silent dup，否则 consume/revoke 按 id 找首个会锚定漂移）。

        Args:
            grant: the grant to store.

        Returns:
            The effective stored grant（dedup 时为既有那张；否则为带最终 ``grant_id``
            的新 grant）。

        Raises:
            ValueError: 显式 ``grant_id`` 与既有不同签名 grant 撞车。
        """
        # 1. 签名 dedup：等价匹配条件已存在 → 复用，避免堆积。
        existing = self.find_equivalent(grant)
        if existing is not None:
            return existing

        # 2. id 唯一性。
        used = {entry.grant.grant_id for entry in self._entries}
        if grant.grant_id:
            if grant.grant_id in used:
                # 同 id 但走到这里 = 签名不同（等价的已在上一步复用）→ 真撞车，拒绝。
                raise ValueError(
                    f"duplicate_grant_id: {grant.grant_id!r} already in use "
                    "by a grant with a different matching signature"
                )
            effective = grant
        else:
            # 自动分配：跳过已占用（含 resume 重种进来的显式 id）。
            new_id = self._next_auto_id(used)
            effective = dataclasses.replace(grant, grant_id=new_id)

        self._entries.append(_GrantEntry(grant=effective, remaining=effective.max_uses))
        return effective

    def find_equivalent(self, grant: PermissionGrant) -> PermissionGrant | None:
        """Return an active grant with the same *matching signature* as ``grant``.

        签名 = 「这张 grant 匹配哪些请求」= (scope, target_pattern, args_match,
        call_chain_prefix, thread_id)；**不含** grant_id / reason / max_uses（「用多久」
        不改变它授权什么）。用于 dedup。
        """
        sig = self._signature(grant)
        for entry in self._entries:
            if self._signature(entry.grant) == sig:
                return entry.grant
        return None

    def _next_auto_id(self, used: set[str]) -> str:
        """分配下一个未占用的确定性 id（``grant-N``）；跳过 used 中已存在的。"""
        if self.id_factory is not None:
            # 注入工厂时由业务保证唯一性（测试用）。
            return self.id_factory()
        while True:
            candidate = f"grant-{self._counter}"
            self._counter += 1
            if candidate not in used:
                return candidate

    @staticmethod
    def _signature(
        grant: PermissionGrant,
    ) -> tuple[str, str, tuple[tuple[str, str], ...] | None, tuple[str, ...], str]:
        """匹配签名（dict 转有序 tuple 以可哈希/可比较）。"""
        args = (
            tuple(sorted(grant.args_match.items()))
            if grant.args_match is not None
            else None
        )
        return (
            grant.scope,
            grant.target_pattern,
            args,
            grant.call_chain_prefix,
            grant.thread_id,
        )

    def match(self, request: PermissionRequest) -> PermissionGrant | None:
        """Return the first active grant matching ``request``, or None.

        Matching = ``PermissionRule.matches`` (scope/target/args) AND
        ``call_chain_prefix`` is a prefix of ``request.call_chain`` AND ``thread_id``
        is unset or equals ``request.thread_id``.
        """
        for entry in self._entries:
            if self._matches(entry.grant, request):
                return entry.grant
        return None

    def consume(self, grant: PermissionGrant) -> bool:
        """Account one use against ``grant``; drop it from the store when exhausted.

        Args:
            grant: the grant returned by ``match`` (resolved by ``grant_id``).

        Returns:
            True if this consume exhausted the grant and it was removed (expired);
            False if it remains active (unbounded, or still has uses left, or not
            found -- a missing grant is treated as already gone).
        """
        for i, entry in enumerate(self._entries):
            if entry.grant.grant_id != grant.grant_id:
                continue
            # 无限次 grant：不计数、永不失效。
            if entry.remaining is None:
                return False
            entry.remaining -= 1
            if entry.remaining <= 0:
                # 用尽 → 移除并报告失效。
                del self._entries[i]
                return True
            return False
        return False

    def revoke(self, grant_id: str) -> bool:
        """Actively remove a grant by id (used by userspace for wall-clock TTL).

        Returns:
            True if a grant was removed; False if no such id existed.
        """
        for i, entry in enumerate(self._entries):
            if entry.grant.grant_id == grant_id:
                del self._entries[i]
                return True
        return False

    def snapshot(self) -> tuple[PermissionGrant, ...]:
        """Return the active grants, each with ``max_uses`` set to its *remaining*
        count, so re-issuing the snapshot restores the exact state on resume (R5).
        """
        return tuple(
            dataclasses.replace(entry.grant, max_uses=entry.remaining)
            for entry in self._entries
        )

    @staticmethod
    def _matches(grant: PermissionGrant, request: PermissionRequest) -> bool:
        """Full grant match: rule part + subtree prefix + thread binding."""
        # 复用 PermissionRule 的三态匹配（literal / re: / glob: + args_match），
        # 不重写匹配逻辑（设计：复用 PermissionRule）。
        rule = PermissionRule(
            scope=grant.scope,
            target_pattern=grant.target_pattern,
            mode="allow",
            args_match=grant.args_match,
        )
        if not rule.matches(request):
            return False
        # call_chain_prefix 子树收窄：空前缀 = 全树；非空必须是请求 call_chain 的前缀。
        prefix = grant.call_chain_prefix
        if prefix and tuple(request.call_chain[: len(prefix)]) != prefix:
            return False
        # thread_id 绑定：空 = 任意 thread；非空必须精确相等。
        return not grant.thread_id or grant.thread_id == request.thread_id
