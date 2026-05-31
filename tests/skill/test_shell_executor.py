"""T3 — ShellScriptExecutor (subprocess) 行为契约。

覆盖：
1. 成功 echo → exit 0 + stdout 含预期
2. 失败 exit_code → 非 0
3. 超时 → SIGTERM/SIGKILL，``is_timeout=True / killed=True``
4. cancel → 立即 kill，``killed=True / is_timeout=False``
5. stdout 截断 → ``truncated=True``，长度 ≤ max_output_bytes
6. cwd 正确（默认 = descriptor.path.parent）
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from taifeng.loop import CancellationToken
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.types import ScriptDescriptor, ScriptInvocation


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _descriptor(
    path: Path,
    *,
    timeout: float = 5.0,
    max_output_bytes: int = 4096,
    args_schema: dict | None = None,
) -> ScriptDescriptor:
    return ScriptDescriptor(
        skill_id="test-skill",
        name=path.stem,
        path=path,
        language="shell",
        timeout_seconds=timeout,
        max_output_bytes=max_output_bytes,
        args_schema=args_schema or {"type": "object"},
    )


async def test_shell_success_echo(tmp_path: Path) -> None:
    script = tmp_path / "hello.sh"
    _write_executable(script, "#!/bin/sh\necho hello-shell\n")
    descriptor = _descriptor(script)
    inv = ScriptInvocation(descriptor=descriptor, args={}, cancel=CancellationToken())
    result = await ShellScriptExecutor().execute(inv)
    assert result.exit_code == 0
    assert "hello-shell" in result.stdout
    assert result.is_timeout is False
    assert result.killed is False
    assert result.truncated is False
    assert result.ok is True


async def test_shell_failure_propagates_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "fail.sh"
    _write_executable(script, "#!/bin/sh\necho boom >&2\nexit 7\n")
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await ShellScriptExecutor().execute(inv)
    assert result.exit_code == 7
    assert "boom" in result.stderr
    assert result.ok is False


async def test_shell_timeout_triggers_sigkill(tmp_path: Path) -> None:
    """trap SIGTERM 死循环 → grace 后 SIGKILL，总耗时 < timeout + grace + 余量。"""
    script = tmp_path / "stubborn.sh"
    _write_executable(
        script,
        "#!/bin/sh\ntrap 'echo got_term' TERM\nwhile :; do sleep 0.1; done\n",
    )
    descriptor = _descriptor(script, timeout=1.0)
    inv = ScriptInvocation(descriptor=descriptor, args={}, cancel=CancellationToken())
    start = time.monotonic()
    result = await ShellScriptExecutor().execute(inv)
    elapsed = time.monotonic() - start
    assert result.is_timeout is True
    assert result.killed is True
    # timeout 1s + grace 1s + 调度松弛 1.5s
    assert elapsed < 3.5, f"timeout did not kill in time: {elapsed}s"


async def test_shell_cancel_kills_running_subprocess(tmp_path: Path) -> None:
    script = tmp_path / "sleep.sh"
    _write_executable(script, "#!/bin/sh\nsleep 30\n")
    descriptor = _descriptor(script, timeout=60.0)
    cancel = CancellationToken()
    inv = ScriptInvocation(descriptor=descriptor, args={}, cancel=cancel)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.3)
        cancel.cancel()

    start = time.monotonic()
    _, result = await asyncio.gather(cancel_soon(), ShellScriptExecutor().execute(inv))
    elapsed = time.monotonic() - start
    assert result.killed is True
    assert result.is_timeout is False
    assert elapsed < 2.5, f"cancel did not kill in time: {elapsed}s"


async def test_shell_stdout_truncation(tmp_path: Path) -> None:
    """输出 1MB，max_output_bytes=2048 → 截断 + truncated=True。"""
    script = tmp_path / "noisy.sh"
    # 1024 个 'a' × 1024 = 1MB
    _write_executable(
        script,
        "#!/bin/sh\nyes a | tr -d '\\n' | head -c 1048576\n",
    )
    descriptor = _descriptor(script, max_output_bytes=2048, timeout=10.0)
    inv = ScriptInvocation(descriptor=descriptor, args={}, cancel=CancellationToken())
    result = await ShellScriptExecutor().execute(inv)
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 2048
    # 不论是否 exit 0 都应当完成读尾
    assert result.is_timeout is False


async def test_shell_cwd_is_script_dir(tmp_path: Path) -> None:
    """默认 cwd = descriptor.path.parent。"""
    skill_dir = tmp_path / "skill-x"
    script = skill_dir / "scripts" / "pwd.sh"
    _write_executable(script, "#!/bin/sh\npwd\n")
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await ShellScriptExecutor().execute(inv)
    # macOS 上 /tmp 可能被解析为 /private/tmp，做兼容
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(
        str(script.parent)
    )


async def test_shell_env_whitelist_excludes_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """父进程 env 含 OPENAI_API_KEY 时，subprocess 看不到该 key。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-this")
    script = tmp_path / "printenv.sh"
    _write_executable(script, "#!/bin/sh\nprintenv OPENAI_API_KEY || true\n")
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await ShellScriptExecutor().execute(inv)
    assert "sk-leak-this" not in result.stdout


async def test_shell_args_no_shell_injection(tmp_path: Path) -> None:
    """args 含 ``"; touch /tmp/pwn"`` 不会被 shell 解析为另一条命令。"""
    script = tmp_path / "echo_arg.sh"
    _write_executable(script, '#!/bin/sh\necho "got=$1"\n')
    descriptor = _descriptor(
        script,
        args_schema={"type": "object", "properties": {"payload": {"type": "string"}}},
    )
    inv = ScriptInvocation(
        descriptor=descriptor,
        args={"payload": "; touch /tmp/pwn-{}".format(os.getpid())},
        cancel=CancellationToken(),
    )
    result = await ShellScriptExecutor().execute(inv)
    assert "; touch" in result.stdout
    pwn_marker = Path(f"/tmp/pwn-{os.getpid()}")
    assert not pwn_marker.exists(), "shell injection executed!"


async def test_shell_stdin_is_closed(tmp_path: Path) -> None:
    """script 试图读 stdin 立即 EOF。"""
    script = tmp_path / "read_stdin.sh"
    _write_executable(
        script,
        '#!/bin/sh\nif read line; then echo "got=$line"; else echo no-stdin; fi\n',
    )
    inv = ScriptInvocation(
        descriptor=_descriptor(script), args={}, cancel=CancellationToken()
    )
    result = await ShellScriptExecutor().execute(inv)
    assert "no-stdin" in result.stdout
