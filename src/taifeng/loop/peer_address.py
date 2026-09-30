"""peer 拓扑路径寻址 —— 按「它在谱系里是谁」而不是「它的 id 是什么」找到对方（ADR 0091）。

直接寻址（thread id / 句柄 id）要求发送方先拿到对方的 id，而 id 是运行时才产生的：兄弟专家之间
互发消息，得靠协调者把句柄逐个转告。拓扑地址用 skill 名指代对方，skill 作者写提示词时就能确定。

| 地址 | 含义 | 谁能用 |
| --- | --- | --- |
| ``parent`` / ``root`` | 本谱系的 root thread | 任何发送方 |
| ``sibling:<skill_id>`` | 另一个分离派发的 child，跑的是该 skill | 分离派发的 child |
| ``child:<skill_id>`` | 分离派发的 child，跑的是该 skill | root |
| ``…#<n>`` | 同一 skill 有多个实例时的第 n 个（按派发先后，从 1 起） | 同上 |

本模块是纯函数：不做 IO，不持有状态，只读调用方给的句柄表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.loop.spawn_handle import SpawnHandle

PeerRelation = Literal["sibling", "child"]

ROOT_ALIASES = frozenset({"parent", "root"})
"""指向本谱系 root thread 的地址。"""

# 已取消 / 出错的实例不参与拓扑寻址（仍可按句柄 id 直接寻址）
_ADDRESSABLE = frozenset({"running", "suspended", "done"})

_ADDRESS = re.compile(r"^(sibling|child):([^#\s]+)(?:#(\d+))?$")


@dataclass(frozen=True)
class TopologyAddress:
    """解析后的拓扑地址。

    Attributes:
        relation: 对方与发送方的关系。
        skill_id: 对方跑的 skill。
        index: 第几个实例（从 1 起）；None = 未指定。
    """

    relation: PeerRelation
    skill_id: str
    index: int | None = None


def is_topology_address(target: str) -> bool:
    """该地址是否属于拓扑寻址的写法（含写错的：以已知关系名加冒号开头即算）。"""
    return target.startswith(("sibling:", "child:"))


def parse_topology_address(target: str) -> TopologyAddress:
    """解析 ``sibling:<skill_id>[#n]`` / ``child:<skill_id>[#n]``。

    Raises:
        ValueError: 写法不合法（``invalid_peer_address``）。
    """
    match = _ADDRESS.match(target)
    if match is None:
        raise ValueError(
            f"invalid_peer_address: {target!r} "
            "(expected sibling:<skill_id>[#n] or child:<skill_id>[#n])"
        )
    relation, skill_id, index = match.group(1), match.group(2), match.group(3)
    if index is not None and int(index) < 1:
        raise ValueError(f"invalid_peer_address: {target!r} (instance index starts at 1)")
    return TopologyAddress(
        relation=relation,  # type: ignore[arg-type]
        skill_id=skill_id,
        index=None if index is None else int(index),
    )


def resolve_topology_address(
    target: str,
    *,
    sender_thread_id: str,
    root_thread_id: str,
    handles: Iterable[SpawnHandle],
) -> str:
    """把拓扑地址解析成目标 thread id。

    Args:
        target: 拓扑地址。
        sender_thread_id: 发送方所在的 thread。
        root_thread_id: 本谱系的 root thread。
        handles: 本谱系的全部派发句柄，按派发先后。

    Raises:
        ValueError: 写法不合法（``invalid_peer_address``）、关系与发送方不符
            （``peer_address_not_applicable``）、没有匹配的实例（``unknown_peer_target``）、
            匹配到多个而未指定第几个（``ambiguous_peer_target``）、序号越界
            （``unknown_peer_target``）。
    """
    address = parse_topology_address(target)
    sender_is_root = sender_thread_id == root_thread_id
    if address.relation == "sibling" and sender_is_root:
        raise ValueError(
            f"peer_address_not_applicable: {target!r} (the root has no siblings; "
            f"use child:{address.skill_id})"
        )
    if address.relation == "child" and not sender_is_root:
        raise ValueError(
            f"peer_address_not_applicable: {target!r} (only the root addresses children; "
            f"use sibling:{address.skill_id})"
        )
    matches = [
        handle for handle in handles
        if handle.skill_id == address.skill_id
        and handle.status in _ADDRESSABLE
        and handle.child_thread_id != sender_thread_id
    ]
    if not matches:
        raise ValueError(f"unknown_peer_target: {target} (no such agent in this lineage)")
    if address.index is not None:
        if address.index > len(matches):
            raise ValueError(
                f"unknown_peer_target: {target} (only {len(matches)} instance(s) of "
                f"{address.skill_id!r})"
            )
        return matches[address.index - 1].child_thread_id
    if len(matches) > 1:
        listed = ", ".join(handle.handle_id for handle in matches)
        raise ValueError(
            f"ambiguous_peer_target: {target} matches {len(matches)} agents "
            f"(handles: {listed}); add #<n> or use a handle id"
        )
    return matches[0].child_thread_id


__all__ = [
    "ROOT_ALIASES",
    "PeerRelation",
    "TopologyAddress",
    "is_topology_address",
    "parse_topology_address",
    "resolve_topology_address",
]
