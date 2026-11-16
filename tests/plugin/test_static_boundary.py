"""R1 静态边界：插件包不含业务标识字段、不读环境变量（plugin-isolation-and-release）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "src" / "taifeng" / "plugin"
_FORBIDDEN = (r"\btenant_id\b", r"\buser_id\b", r"\bos\.getenv\b", r"\bos\.environ\b")


def _violations(text: str) -> list[str]:
    return [pattern for pattern in _FORBIDDEN if re.search(pattern, text)]


def test_plugin_package_has_no_business_fields_or_env_reads() -> None:
    files = sorted(_PLUGIN_DIR.glob("*.py"))
    assert files, "插件包不存在"
    found = {f.name: hits for f in files if (hits := _violations(f.read_text(encoding="utf-8")))}
    assert found == {}


@pytest.mark.parametrize(
    "snippet", ["x = os.getenv('K')", "tenant_id = 1", "os.environ['K']", "def f(user_id): ..."],
)
def test_scanner_catches_violations(snippet: str) -> None:
    assert _violations(snippet)
