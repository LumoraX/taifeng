"""grep 内置工具测试（ADR 0064）：输出模式 / include / 截断 / 二进制与大文件跳过 /
沙盒与符号链接 / 权限 / 取消 / schema，以及真实 EnginePool + SimClient 端到端调用一次。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import taifeng
from taifeng.llm.providers import SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import PermissionDecision, PermissionRequest
from taifeng.tool.arg_validation import check_tool_arguments
from taifeng.tool.builtins import make_glob_tool, make_grep_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(token: CancellationToken | None = None) -> ToolContext:
    """最小工具上下文。"""
    return ToolContext(call_id="c1", cancel=token or CancellationToken(), thread_id="t1")


def _write(root: Path, rel: str, text: str) -> None:
    """在 root 下写文本文件（自动建父目录）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def test_grep_content_mode_with_line_numbers(tmp_path: Path) -> None:
    """content 缺省：路径:行号(1 基):行，按路径排序。"""
    _write(tmp_path, "b.py", "x = 1\nTODO: b\n")
    _write(tmp_path, "a/m.py", "TODO: a\nok\nTODO again\n")
    r = await make_grep_tool(root_dir=tmp_path).handler({"pattern": "TODO"}, _ctx())
    assert not r.is_error
    assert r.output.splitlines() == ["a/m.py:1:TODO: a", "a/m.py:3:TODO again", "b.py:2:TODO: b"]
    assert r.data["count"] == 3
    assert r.data["files_scanned"] == 2


async def test_grep_files_and_count_modes(tmp_path: Path) -> None:
    """files_with_matches 只列文件；count 给出每文件匹配行数。"""
    _write(tmp_path, "a.txt", "hit\nhit\nmiss\n")
    _write(tmp_path, "b.txt", "miss\n")
    _write(tmp_path, "c.txt", "hit\n")
    tool = make_grep_tool(root_dir=tmp_path)
    files = await tool.handler({"pattern": "hit", "output_mode": "files_with_matches"}, _ctx())
    assert files.output.splitlines() == ["a.txt", "c.txt"]
    count = await tool.handler({"pattern": "hit", "output_mode": "count"}, _ctx())
    assert count.output.splitlines() == ["a.txt:2", "c.txt:1"]


async def test_grep_include_ignore_case_and_single_file(tmp_path: Path) -> None:
    """include 按文件名（任意深度）/ 相对路径过滤；ignore_case；path 可指向单个文件。"""
    _write(tmp_path, "src/a.py", "Needle\n")
    _write(tmp_path, "src/a.md", "needle\n")
    _write(tmp_path, "lib/b.py", "needle\n")
    tool = make_grep_tool(root_dir=tmp_path)
    py = await tool.handler({"pattern": "needle", "include": "*.py", "ignore_case": True}, _ctx())
    assert py.output.splitlines() == ["lib/b.py:1:needle", "src/a.py:1:Needle"]
    scoped = await tool.handler({"pattern": "needle", "include": "src/*.{py,md}"}, _ctx())
    assert scoped.output.splitlines() == ["src/a.md:1:needle"]
    single = await tool.handler({"pattern": "eedle", "path": "src/a.py"}, _ctx())
    assert single.output.splitlines() == ["src/a.py:1:Needle"]


async def test_grep_no_match_is_ok(tmp_path: Path) -> None:
    """零命中是正常结果并明确告知。"""
    _write(tmp_path, "a.txt", "nothing here\n")
    r = await make_grep_tool(root_dir=tmp_path).handler({"pattern": "zzz"}, _ctx())
    assert not r.is_error
    assert r.output == "no matches found"


async def test_grep_result_and_line_truncation(tmp_path: Path) -> None:
    """结果条数截断告知；超长单行截断并注明原长度。"""
    _write(tmp_path, "a.txt", "\n".join(f"hit {i}" for i in range(5)))
    _write(tmp_path, "long.txt", "hit " + "y" * 100)
    tool = make_grep_tool(root_dir=tmp_path, max_results=3, max_line_chars=10)
    r = await tool.handler({"pattern": "hit"}, _ctx())
    lines = r.output.splitlines()
    assert lines[:3] == ["a.txt:1:hit 0", "a.txt:2:hit 1", "a.txt:3:hit 2"]
    assert "results truncated at 3 matching lines" in lines[3]
    assert r.data["truncated"] is True
    long = await tool.handler({"pattern": "hit", "path": "long.txt"}, _ctx())
    assert long.output == "long.txt:1:hit yyyyyy …[line truncated, 104 chars]"


async def test_grep_skips_binary_non_utf8_and_large_files(tmp_path: Path) -> None:
    """二进制（含 NUL）/ 非 UTF-8 / 超大文件跳过，且尾注逐类明确告知。"""
    (tmp_path / "bin.dat").write_bytes(b"hit\x00\x01\x02")
    (tmp_path / "latin.txt").write_bytes(b"hit caf\xe9\n")
    _write(tmp_path, "big.log", "hit\n" * 50)
    _write(tmp_path, "ok.txt", "hit\n")
    r = await make_grep_tool(root_dir=tmp_path, max_file_bytes=100).handler(
        {"pattern": "hit"}, _ctx(),
    )
    lines = r.output.splitlines()
    assert lines[0] == "ok.txt:1:hit"
    assert "[skipped 1 file(s) larger than 100 bytes: big.log]" in lines
    assert "[skipped 2 binary or non-UTF-8 file(s)]" in lines
    assert r.data["skipped_binary"] == 2
    assert r.data["skipped_large"] == 1


async def test_grep_rejects_escape_and_ignores_outside_symlinks(tmp_path: Path) -> None:
    """基点逃逸 → sandbox_violation；遍历中指向沙盒外的链接内容不可被搜到。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _write(root, "a.txt", "public\n")
    _write(outside, "secret.txt", "SECRET\n")
    (root / "escape_dir").symlink_to(outside)
    (root / "escape.txt").symlink_to(outside / "secret.txt")
    tool = make_grep_tool(root_dir=root)
    for path in ("../outside", "escape_dir", "escape.txt"):
        r = await tool.handler({"pattern": "SECRET", "path": path}, _ctx())
        assert r.data["reason"] == "sandbox_violation", path
    walk = await tool.handler({"pattern": "SECRET"}, _ctx())
    assert walk.output.splitlines()[0] == "no matches found"
    assert walk.data["skipped_symlinks"] == 2


async def test_grep_bad_args(tmp_path: Path) -> None:
    """非法正则 / 空模式 / 非法 include / 未知输出模式 → bad_args；基点不存在 → not_found。"""
    tool = make_grep_tool(root_dir=tmp_path)
    for args in (
        {"pattern": "("},
        {"pattern": ""},
        {"pattern": "x", "include": "../*.py"},
        {"pattern": "x", "output_mode": "json"},
        {"pattern": "x", "ignore_case": "yes"},
    ):
        r = await tool.handler(args, _ctx())
        assert r.data["reason"] == "bad_args", args
    missing = await tool.handler({"pattern": "x", "path": "nope"}, _ctx())
    assert missing.data["reason"] == "not_found"


async def test_grep_permission_denied(tmp_path: Path) -> None:
    """走 file_read 效果审批；拒绝时不读任何文件。"""
    _write(tmp_path, "a.txt", "hit\n")
    seen: list[PermissionRequest] = []

    class _Deny:
        """记录请求并拒绝。"""

        async def check(self, request: PermissionRequest) -> PermissionDecision:
            seen.append(request)
            return PermissionDecision.deny(reason="no")

    r = await make_grep_tool(root_dir=tmp_path, policy=_Deny()).handler(
        {"pattern": "hit"}, _ctx(),
    )
    assert r.data["reason"] == "permission_denied"
    assert seen[0].scope == "file_read"
    assert seen[0].target == str(tmp_path.resolve())
    assert seen[0].metadata is not None
    assert seen[0].metadata["tool"] == "grep"


async def test_grep_cancelled(tmp_path: Path) -> None:
    """预先取消 → cancelled，不读文件。"""
    _write(tmp_path, "a.txt", "hit\n")
    token = CancellationToken()
    token.cancel()
    r = await make_grep_tool(root_dir=tmp_path).handler({"pattern": "hit"}, _ctx(token))
    assert r.data["reason"] == "cancelled"


def test_grep_spec_metadata_and_schema(tmp_path: Path) -> None:
    """只读并行安全 + pure/none；未知参数与枚举外输出模式被 schema 预校验拒绝。"""
    spec = make_grep_tool(root_dir=tmp_path)
    assert spec.name == "grep"
    assert spec.parallel_safe is True
    assert (spec.effect_kind, spec.reconciliation) == ("pure", "none")
    assert check_tool_arguments(spec.input_schema, {"pattern": "x", "context": 3}) is not None
    assert check_tool_arguments(
        spec.input_schema, {"pattern": "x", "output_mode": "json"},
    ) is not None
    assert check_tool_arguments(
        spec.input_schema,
        {"pattern": "x", "path": "src", "include": "*.py", "ignore_case": True,
         "output_mode": "count"},
    ) is None


# ── 端到端：真实 EnginePool + SimClient，LLM 调用一次 grep ─────────────────────


_SKILL = """---
name: searcher
description: 代码搜索助手
version: 1.0.0
type: composite
entry: true
tool_names: [grep, glob]
max_call_depth: 2
---
# SEARCHER_MARK 在工作区里搜索代码
"""


async def test_grep_e2e_via_engine_pool(tmp_path: Path, threads_dir: Path, sim_client) -> None:
    """extra_tools 显式启用 → 请求里可见 glob / grep；LLM 调一次 grep 拿到真实命中。"""
    skills = tmp_path / "skills"
    (skills / "searcher").mkdir(parents=True)
    (skills / "searcher" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    workspace = tmp_path / "ws"
    _write(workspace, "pkg/core.py", "def target_fn():\n    return 1\n")
    client = sim_client(turns=[
        SimTurn(text="搜一下", tool_calls=[{
            "id": "g1", "name": "grep",
            "arguments": json.dumps({"pattern": "def target_fn", "include": "*.py"}),
        }]),
        SimTurn(text="找到了"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client, compressors=[],
        extra_tools=[make_grep_tool(root_dir=workspace), make_glob_tool(root_dir=workspace)],
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="searcher")
        sub = await engine.submit(taifeng.UserMessage(text="target_fn 在哪"))
        async for ev in engine.subscribe(sub):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed"
                break
        assert {"grep", "glob"} <= client.ledger.requests()[0].tool_names()
        outputs = [
            it.payload for it in engine.history_snapshot()
            if it.kind == "function_call_output" and it.payload["call_id"] == "g1"
        ]
        assert len(outputs) == 1
        assert outputs[0]["is_error"] is False
        assert outputs[0]["output"] == "pkg/core.py:1:def target_fn():"
    finally:
        await pool.close()
