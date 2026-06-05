"""Turn 内任意节点可寻址 rewind 测试。

覆盖统一回访节点模型(turn_root / iteration / dispatch)与 Rewind Op:
- Slice 1：Rewind Op 数据契约(本文件起步)
- Slice 2：RewindCheckpoint 记录(iteration + dispatch 节点)
- Slice 4：_handle_rewind 行为(re_reason / retry_tool / 拒绝路径 / R2/R4/R5)

设计见 docs/superpowers/specs/2026-06-05-addressable-dispatch-rewind-design.md
"""

from __future__ import annotations

import pytest

from taifeng.loop.submission import Rewind, Submission


def test_rewind_op_defaults_and_discriminator() -> None:
    """Rewind Op：kind 鉴别字段固定、mode 默认 re_reason、new_args 默认 None。"""
    op = Rewind(node_id="it3")
    assert op.kind == "rewind"
    assert op.mode == "re_reason"
    assert op.new_args is None
    assert op.node_id == "it3"


def test_rewind_op_accepts_retry_tool_with_args() -> None:
    """retry_tool 模式 + new_args 显式入参可正常构造。"""
    op = Rewind(node_id="disp2", mode="retry_tool", new_args={"skill_id": "x"})
    assert op.mode == "retry_tool"
    assert op.new_args == {"skill_id": "x"}


def test_rewind_op_in_submission_union() -> None:
    """Rewind 并入 Op union —— Submission 能按 discriminator 反序列化它。"""
    sub = Submission.model_validate(
        {"op": {"kind": "rewind", "node_id": "disp0", "mode": "retry_tool"}}
    )
    assert isinstance(sub.op, Rewind)
    assert sub.op.node_id == "disp0"
    assert sub.op.mode == "retry_tool"


def test_rewind_op_rejects_bad_mode() -> None:
    """非法 mode 必须被 pydantic 拒绝(禁 silent fallback)。"""
    with pytest.raises(ValueError):
        Rewind(node_id="x", mode="bogus")  # type: ignore[arg-type]
