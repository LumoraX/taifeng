"""MCP stdio server 经 ``CommandExecutor`` 启动（ADR 0112）：宿主代跑的 server 可以放进沙盒。"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from taifeng import (
    CommandExecutor,
    CommandProcess,
    CommandSpec,
    LocalCommandExecutor,
    McpStdioClient,
    StreamingCommandProcess,
)
from tests.mcp.test_mcp import FAKE_MCP_SERVER

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_server(tmp_path: Path) -> Path:
    path = tmp_path / "fake mcp.py"  # 路径带空格：命令要原样回到 argv
    path.write_text(textwrap.dedent(FAKE_MCP_SERVER), encoding="utf-8")
    return path


class _RecordingExecutor:
    """记录 ``CommandSpec`` 后交给本机执行器——沙盒包装层的位置。"""

    def __init__(self) -> None:
        self.specs: list[CommandSpec] = []
        self.processes: list[CommandProcess] = []

    async def start(self, spec: CommandSpec) -> CommandProcess:
        self.specs.append(spec)
        process = await LocalCommandExecutor().start(spec)
        self.processes.append(process)
        return process


async def test_server_is_started_through_the_injected_executor(fake_server: Path) -> None:
    executor = _RecordingExecutor()
    assert isinstance(executor, CommandExecutor)
    client = await McpStdioClient.spawn(
        [sys.executable, str(fake_server)], executor=executor,
        env={"PATH": os.environ["PATH"], "MARK": "1"}, cwd=str(fake_server.parent),
    )
    try:
        (spec,) = executor.specs
        assert spec.shell is False and spec.stdin is True
        assert spec.env == {"PATH": os.environ["PATH"], "MARK": "1"}
        assert spec.cwd == str(fake_server.parent)
        # argv 经命令文本往返后不变（带空格的路径也不会被拆开）
        import shlex

        assert shlex.split(spec.command) == [sys.executable, str(fake_server)]
        assert client.server_info.get("name") == "fake-mcp"
        result = await client.call_tool("uppercase", {"text": "taifeng"})
        assert result["content"][0]["text"] == "TAIFENG"
    finally:
        await client.close()
    assert executor.processes[0].returncode is not None


async def test_executor_gets_a_minimal_environment_by_default(fake_server: Path) -> None:
    """经执行器启动而不给 env：只有白名单变量，宿主的凭据不会流进沙盒里的 server。"""
    os.environ["TAIFENG_TEST_SECRET"] = "do-not-leak"
    try:
        executor = _RecordingExecutor()
        client = await McpStdioClient.spawn([sys.executable, str(fake_server)], executor=executor)
        await client.close()
    finally:
        del os.environ["TAIFENG_TEST_SECRET"]
    assert "TAIFENG_TEST_SECRET" not in executor.specs[0].env
    assert "PATH" in executor.specs[0].env


class _NoStreams:
    """只满足 ``CommandProcess``、没有流的进程对象（一次性收输出的远端实现）。"""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def test_an_executor_without_streams_is_rejected_and_its_process_killed() -> None:
    process = _NoStreams()

    class _Executor:
        async def start(self, spec: CommandSpec) -> CommandProcess:
            return process

    with pytest.raises(TypeError, match="StreamingCommandProcess"):
        await McpStdioClient.spawn(["server"], executor=_Executor())
    assert process.killed


async def test_executor_start_failure_propagates() -> None:
    class _Broken:
        async def start(self, spec: CommandSpec) -> CommandProcess:
            raise OSError("no sandbox")

    with pytest.raises(OSError, match="no sandbox"):
        await McpStdioClient.spawn(["server"], executor=_Broken())


_CHATTY_SERVER = r"""
import json, sys

sys.stderr.write("x" * (2 * 1024 * 1024) + "\n")   # 远超管道缓冲
sys.stderr.write("last words\n")
sys.stderr.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "chatty", "version": "1"}}}) + "\n")
        sys.stdout.flush()
"""


@pytest.mark.parametrize("through_executor", [False, True])
async def test_a_server_that_floods_stderr_does_not_stall(
    tmp_path: Path, through_executor: bool,
) -> None:
    """server 往 stderr 写满管道不会把自己卡死：客户端持续读走 stderr，只留尾部供排查。"""
    script = tmp_path / "chatty.py"
    script.write_text(_CHATTY_SERVER, encoding="utf-8")
    client = await asyncio.wait_for(
        McpStdioClient.spawn(
            [sys.executable, str(script)],
            executor=LocalCommandExecutor() if through_executor else None,
        ),
        timeout=20,
    )
    try:
        assert client.server_info.get("name") == "chatty"
        for _ in range(200):
            if "last words" in client.stderr_tail:
                break
            await asyncio.sleep(0.01)
        assert client.stderr_tail.rstrip().endswith("last words")
        # 只留尾部：不会把 2MB 都攒在内存里
        assert len(client.stderr_tail) <= 16 * 1024
    finally:
        await client.close()


async def test_local_executor_gives_streams_only_when_asked() -> None:
    env = {"PATH": os.environ["PATH"]}
    piped = await LocalCommandExecutor().start(
        CommandSpec(command="cat", shell=False, cwd=None, env=env, stdin=True))
    assert isinstance(piped, StreamingCommandProcess)
    assert piped.stdin is not None and piped.stdout is not None
    piped.stdin.write(b"ping\n")
    await piped.stdin.drain()
    assert await piped.stdout.readline() == b"ping\n"
    piped.stdin.close()
    assert await asyncio.wait_for(piped.wait(), timeout=5) == 0

    # 没要 stdin 的命令读到的是 EOF，不会去读宿主进程的标准输入
    closed = await LocalCommandExecutor().start(
        CommandSpec(command="cat", shell=False, cwd=None, env=env))
    stdout, _ = await asyncio.wait_for(closed.communicate(), timeout=5)
    assert stdout == b"" and closed.returncode == 0
    assert closed.stdin is None  # type: ignore[attr-defined]


def test_command_spec_stdin_defaults_to_off() -> None:
    assert CommandSpec(command="x", shell=True, cwd=None, env={}).stdin is False
