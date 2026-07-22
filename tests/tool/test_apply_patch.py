"""apply_patch 工具测试（M4 / tool-builtins-extended）。

覆盖 spec ``tool-builtins-extended`` Requirement "apply_patch 工具原子化结构化补丁应用"
的 7 个 Scenario。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.tool.builtins.apply_patch import make_apply_patch_tool
from taifeng.tool.spec import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


@pytest.mark.asyncio
async def test_apply_patch_requests_tool_use_permission(tmp_path: Path) -> None:
    """结构化补丁按工具调用审批，不能发出不存在的 apply_patch scope。"""
    captured: list[PermissionRequest] = []

    async def check(request: PermissionRequest) -> PermissionDecision:
        captured.append(request)
        return PermissionDecision.allow(reason="test")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(check),
    )
    spec = make_apply_patch_tool(root_dir=tmp_path, policy=policy)
    result = await spec.handler(
        {"patches": [{"path": "new.txt", "new_text": "ok", "create": True}]},
        _ctx(),
    )

    assert not result.is_error
    assert [(request.scope, request.target) for request in captured] == [
        ("tool_use", "apply_patch")
    ]


# --------------------------------------------------------------------
# Scenario: edit / create / delete 成功路径
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_edit_patch_succeeds(tmp_path: Path) -> None:
    f = tmp_path / "foo.py"
    f.write_text("def f(x): return x", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{
            "path": "foo.py",
            "old_text": "def f(x): return x",
            "new_text": "def f(x): return x + 1",
        }]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert r.data["applied"][0]["kind"] == "edit"
    assert f.read_text(encoding="utf-8") == "def f(x): return x + 1"


@pytest.mark.asyncio
async def test_apply_create_patch_succeeds(tmp_path: Path) -> None:
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{
            "path": "new.py",
            "new_text": "hello\n",
            "create": True,
        }]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_apply_delete_patch_succeeds(tmp_path: Path) -> None:
    f = tmp_path / "obsolete.py"
    f.write_text("garbage", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{"path": "obsolete.py", "delete": True}]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert not f.exists()


# --------------------------------------------------------------------
# Scenario: 原子性（任一 phase 1 失败 → 0 文件被改）
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_atomic_all_or_nothing(tmp_path: Path) -> None:
    """第 2 个 patch 校验失败，第 1 个不该被应用。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("hello a", encoding="utf-8")
    file_b = tmp_path / "b.py"
    file_b.write_text("hello b", encoding="utf-8")

    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [
            {"path": "a.py", "old_text": "hello a", "new_text": "modified a"},
            # B 的 old_text 不存在 → phase 1 fail
            {"path": "b.py", "old_text": "NONEXISTENT", "new_text": "won't apply"},
        ]},
        _ctx(),
    )
    assert r.is_error
    assert r.data["reason"] == "patch_validation_failed"
    assert r.data["patch_index"] == 1
    # A 应保持原样（原子语义核心）
    assert file_a.read_text(encoding="utf-8") == "hello a"
    assert file_b.read_text(encoding="utf-8") == "hello b"


# --------------------------------------------------------------------
# Scenario: 多次出现 / 沙盒 / create 已存在
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_old_text_ambiguous_fails(tmp_path: Path) -> None:
    f = tmp_path / "dup.py"
    f.write_text("x\nx\nx\n", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{"path": "dup.py", "old_text": "x", "new_text": "y"}]},
        _ctx(),
    )
    assert r.is_error
    assert "ambiguous_old_text" in r.data["error"]
    # 文件未被修改
    assert f.read_text(encoding="utf-8") == "x\nx\nx\n"


@pytest.mark.asyncio
async def test_apply_path_outside_sandbox_fails(tmp_path: Path) -> None:
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{
            "path": "../escape.py",
            "new_text": "evil",
            "create": True,
        }]},
        _ctx(),
    )
    assert r.is_error
    assert "sandbox_violation" in r.data["error"]


@pytest.mark.asyncio
async def test_apply_create_existing_path_fails(tmp_path: Path) -> None:
    f = tmp_path / "exists.py"
    f.write_text("original", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{
            "path": "exists.py",
            "new_text": "overwrite",
            "create": True,
        }]},
        _ctx(),
    )
    assert r.is_error
    assert "path_exists" in r.data["error"]
    # 原文件保持
    assert f.read_text(encoding="utf-8") == "original"
