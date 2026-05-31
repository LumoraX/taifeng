"""Skill 脚本运行时 —— ``ScriptDescriptor`` / ``ScriptExecutor`` 协议与内置实现。

按 ADR 0009 / 能力契约 ``scripts-runtime``：

- SKILL.md ``scripts:`` 声明的可执行入口是 **独立可观测的工具**，不被静默吞进 body
- 业务侧通过 ``ScriptExecutor`` 协议自定义执行器（容器 / 沙箱 / 远程调用），src 内只提供
  ``ShellScriptExecutor`` 与 ``PythonScriptExecutor`` 两个默认 subprocess 实现
- 执行入口走 ``run_script`` 内置工具，与权限层 (``PermissionScope='script_exec'``)
  + Hook 链 (``pre_script_use`` / ``post_script_use``) 完整闭环

参照：``docs/architecture/capabilities/script-execution.md``。
"""

from __future__ import annotations

from taifeng.skill.scripts.executor import ScriptExecutionError, ScriptExecutor
from taifeng.skill.scripts.python import PythonScriptExecutor
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.types import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ScriptDescriptor,
    ScriptInvocation,
    ScriptLanguage,
    ScriptResult,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PythonScriptExecutor",
    "ScriptDescriptor",
    "ScriptExecutionError",
    "ScriptExecutor",
    "ScriptInvocation",
    "ScriptLanguage",
    "ScriptResult",
    "ShellScriptExecutor",
]
