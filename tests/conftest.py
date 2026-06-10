"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

ATOMIC_SKILL = """---
name: style-checker
description: 代码风格审查
version: 1.0.0
type: atomic
---
# 风格审查
按规范审查 diff，列出违规处。
"""

COMPOSITE_SKILL = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是一位代码审查专家。
"""


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    (skills / "style-checker").mkdir(parents=True)
    (skills / "style-checker" / "SKILL.md").write_text(ATOMIC_SKILL, encoding="utf-8")
    (skills / "code-reviewer").mkdir(parents=True)
    (skills / "code-reviewer" / "SKILL.md").write_text(COMPOSITE_SKILL, encoding="utf-8")
    return skills


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    p = tmp_path / "threads"
    p.mkdir()
    return p


@pytest.fixture
def sim_client():
    """SimClient/RoutingSimClient 工厂 fixture —— 收尾自动断言无合同违规。

    D4 双保险：即使 ``SimContractViolation`` 被引擎兜底路径吞掉转成 turn_failed，
    teardown 的 violations 断言仍能让测试红。

    用法：
        client = sim_client(turns=[SimTurn(...)])              # 顺序回放
        client = sim_client(routes={"MARK": [SimTurn(...)]})   # 标记路由
    """
    created: list = []

    def factory(*, turns=None, routes=None, **kwargs):
        from taifeng.llm.providers.sim import RoutingSimClient, SimClient

        if routes is not None:
            client = RoutingSimClient(routes=routes, **kwargs)
        else:
            client = SimClient(turns=list(turns or []), **kwargs)
        created.append(client)
        return client

    yield factory
    for client in created:
        leftovers = [str(v) for v in client.ledger.violations]
        assert not leftovers, f"sim 合同违规未处理: {leftovers}"
