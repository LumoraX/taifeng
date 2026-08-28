"""校验 Taifeng wheel/sdist metadata 并执行干净安装 smoke。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version


class ReleaseArtifactError(RuntimeError):
    """发行产物不满足发布契约。"""


def _read_wheel_metadata(path: Path) -> str:
    """读取 wheel 中唯一的 Core Metadata。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise ReleaseArtifactError(
                    f"wheel must contain exactly one METADATA: {path.name}"
                )
            return archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ReleaseArtifactError(f"invalid wheel: {path.name}") from exc


def _read_sdist_metadata(path: Path) -> str:
    """读取 sdist 中唯一的 PKG-INFO。"""
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith("/PKG-INFO")
            ]
            if len(members) != 1:
                raise ReleaseArtifactError(
                    f"sdist must contain exactly one PKG-INFO: {path.name}"
                )
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise ReleaseArtifactError(f"cannot read PKG-INFO: {path.name}")
            return extracted.read().decode("utf-8")
    except (OSError, UnicodeError, tarfile.TarError) as exc:
        raise ReleaseArtifactError(f"invalid sdist: {path.name}") from exc


def has_required_sniffio_dependency(requirements: list[str]) -> bool:
    """判断 metadata 是否无条件声明 ``sniffio>=1.3``。"""
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        if requirement.name.lower().replace("_", "-") != "sniffio":
            continue
        if requirement.marker is not None:
            continue
        if any(
            specifier.operator in {">=", "~="}
            and Version(specifier.version) >= Version("1.3")
            for specifier in requirement.specifier
        ):
            return True
    return False


def verify_artifact_metadata(path: Path, *, expected_version: str) -> None:
    """校验单个 wheel 或 sdist 的名称、版本与直接依赖。"""
    if path.suffix == ".whl":
        raw = _read_wheel_metadata(path)
    elif path.name.endswith(".tar.gz"):
        raw = _read_sdist_metadata(path)
    else:
        raise ReleaseArtifactError(f"unsupported artifact: {path.name}")
    metadata = Parser().parsestr(raw)
    if metadata.get("Name", "").lower() != "taifeng":
        raise ReleaseArtifactError(f"unexpected project name in {path.name}")
    if metadata.get("Version") != expected_version:
        raise ReleaseArtifactError(f"version mismatch in {path.name}")
    requirements = metadata.get_all("Requires-Dist", [])
    if not has_required_sniffio_dependency(requirements):
        raise ReleaseArtifactError(f"sniffio dependency missing in {path.name}")


def _venv_python(venv: Path) -> Path:
    """返回当前平台虚拟环境的 Python 路径。"""
    unix = venv / "bin" / "python"
    return unix if unix.exists() else venv / "Scripts" / "python.exe"


def _clean_environment() -> dict[str, str]:
    """移除会绕过虚拟环境 site-packages 的 Python 注入变量。"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


def _run(
    command: list[str],
    *,
    label: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> None:
    """执行发布门禁子命令并把失败收敛为稳定异常。"""
    try:
        subprocess.run(  # noqa: S603 —— 参数列表由本脚本固定构造
            command,
            check=True,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseArtifactError(f"{label} failed") from exc


def smoke_install_artifact(path: Path, *, expected_version: str) -> None:
    """在全新虚拟环境中安装产物并验证公共导入。"""
    with tempfile.TemporaryDirectory(prefix="taifeng-release-smoke-") as td:
        workdir = Path(td)
        venv = workdir / "venv"
        environment = _clean_environment()
        _run(
            ["uv", "venv", str(venv)],
            label=f"create venv for {path.name}",
            env=environment,
            cwd=workdir,
        )
        python = _venv_python(venv)
        _run(
            ["uv", "pip", "install", "--python", str(python), str(path.resolve())],
            label=f"install {path.name}",
            env=environment,
            cwd=workdir,
        )
        smoke = """
from importlib.metadata import version
from pathlib import Path
import sniffio
import taifeng
from taifeng.llm.providers.codex import CodexResponsesClient
expected = sys.argv[1]
assert taifeng.__version__ == version("taifeng") == expected
assert CodexResponsesClient.__name__ == "CodexResponsesClient"
assert Path(taifeng.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
"""
        _run(
            [str(python), "-I", "-c", "import sys\n" + smoke, expected_version],
            label=f"import smoke for {path.name}",
            env=environment,
            cwd=workdir,
        )


def _find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    """要求输出目录恰好包含一个 wheel 与一个 sdist。"""
    wheels = sorted(dist_dir.glob("taifeng-*.whl"))
    sdists = sorted(dist_dir.glob("taifeng-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseArtifactError(
            f"expected one wheel and one sdist, got {len(wheels)} and {len(sdists)}"
        )
    return wheels[0], sdists[0]


def verify_release(dist_dir: Path, *, expected_version: str) -> None:
    """执行完整发行候选门禁。"""
    wheel, sdist = _find_artifacts(dist_dir)
    for artifact in (wheel, sdist):
        verify_artifact_metadata(artifact, expected_version=expected_version)
        print(f"[metadata PASS] {artifact.name}")
        smoke_install_artifact(artifact, expected_version=expected_version)
        print(f"[install PASS] {artifact.name}")


def main() -> int:
    """解析 CLI 参数并返回进程退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        verify_release(args.dist_dir, expected_version=args.expected_version)
    except ReleaseArtifactError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print("[summary] release artifacts PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
