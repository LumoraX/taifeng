"""派生子进程的最小 env 白名单 —— 内核内唯一实现。

为什么需要：`env=None` 交给 `asyncio.create_subprocess_*` 意味着**继承宿主完整
`os.environ`**，子进程因此能读到 API key、数据库口令等全部凭据。安全的默认必须
是默认，而不是一个需要业务记得去传的开关。

消费方：`tool/builtins/shell.py`（shell_exec）、`tool/builtins/background.py`
（run_in_background）、`skill/scripts/shell.py`（ShellScriptExecutor）。三者共用
本模块，避免同一份安全判据出现第二个副本而各自漂移。
"""

from __future__ import annotations

import os

# 白名单 —— 子进程仅可见这几个 system-level 变量。
# 注：这里读 os.environ 取的是 subprocess 启动所需的**系统**环境（PATH 等），
# 不是业务配置（业务配置一律依赖注入，见 CLAUDE.md「src/ 内禁止 os.getenv」）。
SAFE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG")


def default_safe_env() -> dict[str, str]:
    """构建仅含白名单 key 的最小 env 字典。

    Returns:
        白名单变量 + 强制 ``LC_ALL`` / ``LANG`` 为 ``C.UTF-8``（稳定子进程的
        locale 输出，避免同一命令在不同宿主 locale 下产出不同编码）。
    """
    env: dict[str, str] = {}
    for key in SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.setdefault("LC_ALL", "C.UTF-8")
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"
    return env
