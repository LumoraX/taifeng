"""内置 file_read / file_write / shell_exec 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from taifeng.tool.builtins import (
    make_file_read_tool,
    make_file_write_tool,
    make_shell_exec_tool,
)
from taifeng.tool.spec import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


@pytest.mark.asyncio
async def test_file_read_in_sandbox(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello, taifeng", encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "hello.txt"}, _ctx())
    assert not r.is_error
    assert "hello, taifeng" in r.output


@pytest.mark.asyncio
async def test_file_read_blocks_escape(tmp_path: Path) -> None:
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "../../etc/passwd"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "sandbox_violation"


@pytest.mark.asyncio
async def test_file_write_and_read_roundtrip(tmp_path: Path) -> None:
    writer = make_file_write_tool(root_dir=tmp_path)
    reader = make_file_read_tool(root_dir=tmp_path)

    w = await writer.handler({"path": "out/data.txt", "content": "abc"}, _ctx())
    assert not w.is_error
    r = await reader.handler({"path": "out/data.txt"}, _ctx())
    assert not r.is_error
    assert r.output == "abc"


@pytest.mark.asyncio
async def test_file_read_offset_limit_reads_line_range(tmp_path: Path) -> None:
    """offset/limit 按行分页:读取指定行区间。"""
    (tmp_path / "log.txt").write_text("L0\nL1\nL2\nL3\nL4", encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "log.txt", "offset": 1, "limit": 2}, _ctx())
    assert not r.is_error
    assert r.output == "L1\nL2"


@pytest.mark.asyncio
async def test_file_read_limit_beyond_eof_returns_remaining(tmp_path: Path) -> None:
    """limit 超过剩余行数:返回到文件末尾,不报错。"""
    (tmp_path / "log.txt").write_text("L0\nL1\nL2\nL3\nL4", encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "log.txt", "offset": 0, "limit": 100}, _ctx())
    assert not r.is_error
    assert r.output == "L0\nL1\nL2\nL3\nL4"


@pytest.mark.asyncio
async def test_file_read_offset_out_of_range_returns_empty(tmp_path: Path) -> None:
    """offset 越界:返回空内容而非报错(便于 LLM 探测分页边界)。"""
    (tmp_path / "log.txt").write_text("L0\nL1\nL2", encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "log.txt", "offset": 10, "limit": 5}, _ctx())
    assert not r.is_error
    assert r.output == ""


@pytest.mark.asyncio
async def test_file_read_no_paging_returns_whole_file(tmp_path: Path) -> None:
    """省略 offset/limit:行为与旧版一致(整文件原样返回)。"""
    content = "L0\nL1\nL2\nL3\nL4"
    (tmp_path / "log.txt").write_text(content, encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "log.txt"}, _ctx())
    assert not r.is_error
    assert r.output == content


@pytest.mark.asyncio
async def test_file_read_offset_only_reads_to_end(tmp_path: Path) -> None:
    """仅给 offset、不给 limit:从 offset 行读到末尾。"""
    (tmp_path / "log.txt").write_text("L0\nL1\nL2\nL3", encoding="utf-8")
    spec = make_file_read_tool(root_dir=tmp_path)
    r = await spec.handler({"path": "log.txt", "offset": 2}, _ctx())
    assert not r.is_error
    assert r.output == "L2\nL3"


@pytest.mark.asyncio
async def test_file_read_permission_denied(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    policy = PermissionPolicy(
        rules=[
            PermissionRule(scope="file_read", target_pattern="re:secret", mode="deny"),
        ],
        default_mode="allow",
    )
    spec = make_file_read_tool(root_dir=tmp_path, policy=policy)
    r = await spec.handler({"path": "secret.txt"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_shell_exec_requires_policy() -> None:
    spec = make_shell_exec_tool(policy=None)
    r = await spec.handler({"command": "echo hi"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "no_policy"


@pytest.mark.asyncio
async def test_shell_exec_safety_blocks_rm() -> None:
    policy = PermissionPolicy(default_mode="allow")
    spec = make_shell_exec_tool(policy=policy)
    r = await spec.handler({"command": "rm -rf /tmp/foo"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "safety_blocked"


@pytest.mark.asyncio
async def test_shell_exec_runs_safe_command() -> None:
    policy = PermissionPolicy(default_mode="allow")
    spec = make_shell_exec_tool(policy=policy, allow_shell=False)
    r = await spec.handler({"command": "echo taifeng-ok"}, _ctx())
    assert not r.is_error
    assert "taifeng-ok" in r.output
    assert r.data["exit_code"] == 0


@pytest.mark.asyncio
async def test_shell_exec_permission_denied() -> None:
    async def cb(req: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny(reason="user_no")

    policy = PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(cb))
    spec = make_shell_exec_tool(policy=policy)
    r = await spec.handler({"command": "echo hi"}, _ctx())
    assert r.is_error
    assert r.data["reason"] == "permission_denied"
