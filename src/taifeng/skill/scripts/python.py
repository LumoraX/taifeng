"""``PythonScriptExecutor`` —— ``language="python"`` 的默认 subprocess 实现。

复用 ``ShellScriptExecutor`` 的所有 subprocess 隔离逻辑（env 白名单 / stdin 关闭 /
process group kill / 输出截断 / cancel 传播 / timeout 强制），仅替换 argv 第一项
为指定 Python 解释器。

业务侧若要用自定义 venv，传入 ``PythonScriptExecutor(python_bin=Path(".venv/bin/python"))``。
"""

from __future__ import annotations

import sys
from pathlib import Path

from taifeng.skill.scripts.executor import ScriptExecutor
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.types import ScriptInvocation


class PythonScriptExecutor(ShellScriptExecutor):
    """``language="python"`` 默认实现。

    Args:
        python_bin: Python 解释器路径；不传则使用 ``sys.executable``
        env / extra_env: 同 ``ShellScriptExecutor``
    """

    def __init__(
        self,
        *,
        python_bin: Path | str | None = None,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(env=env, extra_env=extra_env, shell_binary=None)
        self._python_bin = str(python_bin) if python_bin is not None else sys.executable

    def _build_argv(self, inv: ScriptInvocation) -> list[str]:
        script_path = inv.descriptor.path.as_posix()
        from taifeng.skill.scripts.shell import _format_args_to_argv

        args = _format_args_to_argv(inv)
        return [self._python_bin, script_path, *args]


# === Protocol 注册检查 ===
def _assert_protocol() -> None:
    _x: ScriptExecutor = PythonScriptExecutor()  # noqa: F841


__all__ = ["PythonScriptExecutor"]
