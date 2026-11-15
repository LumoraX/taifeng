"""examples/plugin_system/demo.py 的回归：示例的每条核验都必须成立（plugin-isolation-and-release）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_DEMO = Path(__file__).resolve().parents[2] / "examples" / "plugin_system" / "demo.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_system_demo", _DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_demo_checks_all_pass(tmp_path: Path) -> None:
    demo = _load()
    result = await demo.run(tmp_path)
    assert demo.report(result) is True
    assert result["pool_events"] == [
        "plugin_loaded", "plugin_loaded", "plugin_loaded", "plugin_unloaded", "plugin_unloaded",
    ]
    lines = (tmp_path / "pool-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


async def test_report_detects_a_missing_capability(tmp_path: Path) -> None:
    """核验不是摆设：工具没被撤销时 report 判失败。"""
    demo = _load()
    result = await demo.run(tmp_path)
    result["tool_after_close"] = True
    assert demo.report(result) is False
