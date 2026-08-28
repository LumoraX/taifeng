"""发行包 metadata 契约测试。"""

from __future__ import annotations

from importlib.metadata import requires


def test_distribution_declares_sniffio_runtime_dependency() -> None:
    """源码直接导入的 sniffio 必须是核心直接依赖。"""
    dependencies = requires("taifeng") or []
    assert any(item.startswith("sniffio>=1.3") for item in dependencies)
