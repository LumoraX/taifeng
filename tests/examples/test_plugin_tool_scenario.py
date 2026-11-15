"""真实回归 plugin_tool 场景的判定能检出「插件没加载」（plugin-isolation-and-release）。"""

from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _modules(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """加载矩阵与 selfcheck 模块，禁止读取任何 dotenv 文件。"""
    monkeypatch.syspath_prepend(str(_ROOT / "examples"))
    monkeypatch.syspath_prepend(str(_ROOT / "examples" / "real_llm"))
    bootstrap = importlib.import_module("_provider_bootstrap")
    monkeypatch.setattr(bootstrap, "load_dotenv_files", lambda *paths: [])
    return (
        importlib.import_module("capability_matrix"),
        importlib.import_module("selfcheck"),
        importlib.import_module("_setups"),
    )


async def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, with_plugin: bool) -> str:
    matrix, selfcheck, setups = _modules(monkeypatch)
    from taifeng.llm.providers import RoutingSimClient

    scenario = next(sc for sc in matrix.SCENARIOS if sc.demo_id == "plugin_tool")
    scenario = dataclasses.replace(
        scenario, setup=lambda root, client: setups.setup_plugin_tool(root, client, with_plugin=with_plugin),
    )
    client = RoutingSimClient(routes=selfcheck.SIM_ROUTES["plugin_tool"])
    result = await matrix.run_scenario(client, scenario, tmp_path / "logs")
    return matrix._verdict(result)[0]


async def test_scenario_passes_with_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _run(monkeypatch, tmp_path, with_plugin=True) == "✅PASS"


async def test_scenario_fails_without_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _run(monkeypatch, tmp_path, with_plugin=False) != "✅PASS"
