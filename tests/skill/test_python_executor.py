"""T4 — PythonScriptExecutor (sys.executable subprocess) 行为契约。"""

from __future__ import annotations

import sys
from pathlib import Path

from taifeng.loop import CancellationToken
from taifeng.skill.scripts.python import PythonScriptExecutor
from taifeng.skill.scripts.types import ScriptDescriptor, ScriptInvocation


def _write_script(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _descriptor(path: Path, *, timeout: float = 5.0) -> ScriptDescriptor:
    return ScriptDescriptor(
        skill_id="test-skill",
        name=path.stem,
        path=path,
        language="python",
        timeout_seconds=timeout,
    )


async def test_python_success_prints_json(tmp_path: Path) -> None:
    """正常 print(json.dumps(...)) 退出 0。"""
    script = tmp_path / "ok.py"
    _write_script(
        script,
        'import json\nprint(json.dumps({"hello": "world"}))\n',
    )
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await PythonScriptExecutor().execute(inv)
    assert result.exit_code == 0
    assert '"hello": "world"' in result.stdout
    assert result.ok is True


async def test_python_exception_nonzero_exit(tmp_path: Path) -> None:
    """raise → 非 0 退出 + stderr 含 traceback。"""
    script = tmp_path / "boom.py"
    _write_script(script, 'raise RuntimeError("boom")\n')
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await PythonScriptExecutor().execute(inv)
    assert result.exit_code != 0
    assert "RuntimeError" in result.stderr
    assert "boom" in result.stderr


async def test_python_timeout_killed(tmp_path: Path) -> None:
    """死循环 + timeout=1s → killed=True / is_timeout=True。"""
    script = tmp_path / "loop.py"
    _write_script(script, "import time\nwhile True: time.sleep(0.1)\n")
    inv = ScriptInvocation(
        descriptor=_descriptor(script, timeout=1.0),
        args={},
        cancel=CancellationToken(),
    )
    result = await PythonScriptExecutor().execute(inv)
    assert result.is_timeout is True
    assert result.killed is True


async def test_python_custom_python_bin(tmp_path: Path) -> None:
    """显式传 python_bin 时使用该解释器。"""
    script = tmp_path / "version.py"
    _write_script(script, "import sys\nprint(sys.executable)\n")
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    executor = PythonScriptExecutor(python_bin=sys.executable)
    result = await executor.execute(inv)
    assert result.exit_code == 0
    assert sys.executable in result.stdout


async def test_python_argv_passes_args(tmp_path: Path) -> None:
    """args 通过 argv 传给 sys.argv，按 args_schema.properties 顺序展开。"""
    script = tmp_path / "echo.py"
    _write_script(script, "import sys\nprint(sys.argv[1:])\n")
    descriptor = ScriptDescriptor(
        skill_id="test",
        name="echo",
        path=script,
        language="python",
        timeout_seconds=5.0,
        args_schema={
            "type": "object",
            "properties": {
                "first": {"type": "string"},
                "second": {"type": "string"},
            },
        },
    )
    inv = ScriptInvocation(
        descriptor=descriptor,
        args={"first": "alpha", "second": "beta"},
        cancel=CancellationToken(),
    )
    result = await PythonScriptExecutor().execute(inv)
    assert result.exit_code == 0
    assert "'alpha'" in result.stdout
    assert "'beta'" in result.stdout
