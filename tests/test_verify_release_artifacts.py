"""PyPI 发行产物门禁测试。"""

from __future__ import annotations

import io
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from scripts.verify_release_artifacts import ReleaseArtifactError, verify_artifact_metadata

if TYPE_CHECKING:
    from pathlib import Path


def _metadata(*, version: str, include_sniffio: bool = True) -> str:
    """构造最小 Core Metadata。"""
    dependency = "Requires-Dist: sniffio>=1.3\n" if include_sniffio else ""
    return (
        "Metadata-Version: 2.4\n"
        "Name: taifeng\n"
        f"Version: {version}\n"
        f"{dependency}\n"
    )


def _write_wheel(path: Path, metadata: str) -> None:
    """写入仅供 metadata 解析的最小 wheel。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("taifeng-2026.8.28.0.dist-info/METADATA", metadata)


def _write_sdist(path: Path, metadata: str) -> None:
    """写入仅供 metadata 解析的最小 sdist。"""
    body = metadata.encode()
    info = tarfile.TarInfo("taifeng-2026.8.28.0/PKG-INFO")
    info.size = len(body)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(body))


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_verify_artifact_metadata_accepts_release_contract(
    tmp_path: Path,
    kind: str,
) -> None:
    """wheel 与 sdist 都必须携带正确版本和 sniffio 依赖。"""
    suffix = ".whl" if kind == "wheel" else ".tar.gz"
    artifact = tmp_path / f"taifeng-2026.8.28.0{suffix}"
    writer = _write_wheel if kind == "wheel" else _write_sdist
    writer(artifact, _metadata(version="2026.8.28.0"))

    verify_artifact_metadata(artifact, expected_version="2026.8.28.0")


@pytest.mark.parametrize(
    ("version", "include_sniffio", "message"),
    [
        ("2026.8.7.0", True, "version"),
        ("2026.8.28.0", False, "sniffio"),
    ],
)
def test_verify_artifact_metadata_rejects_invalid_contract(
    tmp_path: Path,
    version: str,
    include_sniffio: bool,
    message: str,
) -> None:
    """版本漂移或缺失直接依赖必须 fail closed。"""
    artifact = tmp_path / "taifeng-2026.8.28.0-py3-none-any.whl"
    _write_wheel(
        artifact,
        _metadata(version=version, include_sniffio=include_sniffio),
    )

    with pytest.raises(ReleaseArtifactError, match=message):
        verify_artifact_metadata(artifact, expected_version="2026.8.28.0")
