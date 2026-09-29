"""发行包 metadata 契约测试。"""

from __future__ import annotations

from importlib.metadata import requires
from importlib.resources import files

from scripts.verify_release_artifacts import has_required_sniffio_dependency


def test_distribution_declares_sniffio_runtime_dependency() -> None:
    """源码直接导入的 sniffio 必须是核心直接依赖。"""
    dependencies = requires("taifeng") or []
    assert has_required_sniffio_dependency(dependencies)


def test_package_ships_pep561_typed_marker() -> None:
    """包内必须带 ``py.typed``（PEP 561）。

    缺了它，下游适配包在 mypy strict 下 import taifeng 会报 ``import-untyped``，
    整个稳定层的类型注解对下游失效。发行产物里是否真的带上，由
    ``scripts/verify_release_artifacts.py`` 的干净安装冒烟再验一次。
    """
    assert files("taifeng").joinpath("py.typed").is_file()
