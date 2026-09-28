"""公共 API 弃用机制（ADR 0066）：顶层符号改名 / 迁移时的过渡别名。

稳定层符号不直接删除：先登记到 ``DEPRECATED_ALIASES``，由 ``taifeng.__getattr__`` 解析——
访问旧名照常可用，但发出 ``DeprecationWarning`` 写明起始版本、最早移除时间与替代写法。
至少跨两个发布版本且不少于 30 天后，才可删除别名（同时更新 API 快照）。

参照：CPython PEP 562 模块 ``__getattr__`` 的弃用惯例。
"""

from __future__ import annotations

import importlib
import warnings
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeprecatedAlias:
    """一个处于弃用期的顶层旧名。

    Attributes:
        target: 新位置，``"模块路径:属性名"``。
        since: 开始弃用的发布版本。
        removal_not_before: 最早可移除的日期（``YYYY-MM-DD``）。
        replacement: 给使用者的替代写法（出现在告警文本里）。
    """

    target: str
    since: str
    removal_not_before: str
    replacement: str


DEPRECATED_ALIASES: dict[str, DeprecatedAlias] = {}
"""顶层旧名 → 弃用信息。当前为空：尚无处于弃用期的稳定层符号。"""


def resolve_deprecated(name: str) -> Any:
    """解析弃用别名：发 ``DeprecationWarning`` 并返回新位置的对象。

    Raises:
        KeyError: ``name`` 不在弃用登记表中（调用方据此转为 AttributeError）。
    """
    alias = DEPRECATED_ALIASES[name]
    warnings.warn(
        f"taifeng.{name} is deprecated since {alias.since} and may be removed after "
        f"{alias.removal_not_before}; use {alias.replacement} instead",
        DeprecationWarning,
        stacklevel=3,
    )
    module_path, attr = alias.target.split(":", 1)
    return getattr(importlib.import_module(module_path), attr)
