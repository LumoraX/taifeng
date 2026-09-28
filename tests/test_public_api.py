"""public-api —— 稳定层 / 实验层 / 弃用机制（ADR 0066）。

稳定层 = ``taifeng.__all__``。快照守护：任何增删都必须同步改 ``tests/public_api_snapshot.txt``，
让 API 变化成为有意为之的决定；移除须先走弃用期（``taifeng._deprecation``）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import taifeng
import taifeng.experimental
from taifeng import _deprecation
from taifeng._deprecation import DeprecatedAlias

_SNAPSHOT = Path(__file__).parent / "public_api_snapshot.txt"


def test_stable_api_matches_snapshot() -> None:
    """顶层 ``__all__`` 与快照一致；不一致时提示按 ADR 0066 处理。"""
    recorded = set(_SNAPSHOT.read_text(encoding="utf-8").split())
    current = set(taifeng.__all__)
    assert current == recorded, (
        f"public API changed — added {sorted(current - recorded)}, removed "
        f"{sorted(recorded - current)}. Update tests/public_api_snapshot.txt deliberately; "
        "removing a stable name requires a deprecation period (ADR 0066)."
    )


@pytest.mark.parametrize("module", [taifeng, taifeng.experimental], ids=["stable", "experimental"])
def test_every_exported_name_resolves(module: object) -> None:
    for name in module.__all__:  # type: ignore[attr-defined]
        if name.startswith("Otel"):
            continue  # optional extra，按需 lazy import（未装 extra 时访问会在构造期报错）
        assert getattr(module, name) is not None


def test_experimental_names_are_not_in_stable_layer() -> None:
    """同一符号不能同时处在两层（晋升时从实验层移到顶层）。"""
    assert not set(taifeng.experimental.__all__) & set(taifeng.__all__)


def test_deprecated_alias_warns_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """弃用期旧名照常可用，但发 DeprecationWarning 写明替代写法。"""
    monkeypatch.setitem(_deprecation.DEPRECATED_ALIASES, "OldBudget", DeprecatedAlias(
        target="taifeng.context.budget:ContextBudget", since="2026.9.29",
        removal_not_before="2026-11-01", replacement="taifeng.ContextBudget"))
    with pytest.warns(DeprecationWarning, match="use taifeng.ContextBudget instead"):
        resolved = taifeng.OldBudget  # type: ignore[attr-defined]
    assert resolved is taifeng.ContextBudget


def test_unknown_attribute_still_raises() -> None:
    with pytest.raises(AttributeError):
        taifeng.NoSuchThing  # type: ignore[attr-defined]  # noqa: B018


def test_deprecation_registry_targets_exist() -> None:
    """登记中的弃用别名都能解析到真实对象（防止别名指向已删除的位置）。"""
    for alias in _deprecation.DEPRECATED_ALIASES.values():
        module_path, attr = alias.target.split(":", 1)
        assert hasattr(importlib.import_module(module_path), attr)
