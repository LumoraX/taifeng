"""PyPI 发行产物门禁测试。"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import verify_release_artifacts as release_artifacts
from scripts.verify_release_artifacts import ReleaseArtifactError, verify_artifact_metadata


def _metadata(
    *,
    version: str,
    sniffio_requirement: str | None = "sniffio>=1.3",
) -> str:
    """构造最小 Core Metadata。"""
    dependency = (
        f"Requires-Dist: {sniffio_requirement}\n"
        if sniffio_requirement is not None
        else ""
    )
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
    ("version", "sniffio_requirement", "message"),
    [
        ("2026.8.7.0", "sniffio>=1.3", "version"),
        ("2026.8.28.0", None, "sniffio"),
        (
            "2026.8.28.0",
            "sniffio>=1.3; python_version < '3.0'",
            "sniffio",
        ),
    ],
)
def test_verify_artifact_metadata_rejects_invalid_contract(
    tmp_path: Path,
    version: str,
    sniffio_requirement: str | None,
    message: str,
) -> None:
    """版本漂移或缺失直接依赖必须 fail closed。"""
    artifact = tmp_path / "taifeng-2026.8.28.0-py3-none-any.whl"
    _write_wheel(
        artifact,
        _metadata(version=version, sniffio_requirement=sniffio_requirement),
    )

    with pytest.raises(ReleaseArtifactError, match=message):
        verify_artifact_metadata(artifact, expected_version="2026.8.28.0")


def test_smoke_install_isolates_import_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装 smoke 不得被调用者 PYTHONPATH/PYTHONHOME 污染。"""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def capture(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    monkeypatch.setattr(release_artifacts, "_run", capture)
    monkeypatch.setenv("PYTHONPATH", "/poisoned/source")
    monkeypatch.setenv("PYTHONHOME", "/poisoned/home")

    release_artifacts.smoke_install_artifact(
        tmp_path / "taifeng-2026.8.28.0-py3-none-any.whl",
        expected_version="2026.8.28.0",
    )

    smoke_command, smoke_options = calls[-1]
    assert "-I" in smoke_command
    environment = smoke_options["env"]
    assert isinstance(environment, dict)
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert smoke_options["cwd"] != Path.cwd()


def test_publish_workflow_separates_verification_from_oidc() -> None:
    """仓库代码只能在无 OIDC 的 verify job 中运行。"""
    workflow_path = Path(".github/workflows/publish.yml")
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert set(jobs) == {"verify", "publish"}
    assert jobs["publish"]["needs"] == "verify"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert "permissions" not in jobs["verify"]
    verify_steps = jobs["verify"]["steps"]
    tag_index = next(
        index for index, step in enumerate(verify_steps) if step["name"] == "校验 tag 与 main"
    )
    uv_index = next(
        index for index, step in enumerate(verify_steps) if step["name"] == "安装 uv"
    )
    assert tag_index < uv_index
    tag_script = verify_steps[tag_index]["run"]
    assert "GITHUB_SHA" in tag_script
    assert "origin/main" in tag_script
