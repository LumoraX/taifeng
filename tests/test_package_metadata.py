"""发行包 metadata 契约测试。"""

from __future__ import annotations

from importlib.metadata import requires

from scripts.verify_release_artifacts import has_required_sniffio_dependency


def test_distribution_declares_sniffio_runtime_dependency() -> None:
    """源码直接导入的 sniffio 必须是核心直接依赖。"""
    dependencies = requires("taifeng") or []
    assert has_required_sniffio_dependency(dependencies)
