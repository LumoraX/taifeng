"""examples/ 冒烟核验脚本的分档与判定逻辑测试。

守的是两件事:
  1. **分档规则**别悄悄漂移 —— 分错档的直接后果是示例被静默跳过然后烂掉
     (``examples/mcp_basic`` 的真实事故);
  2. **失败判定**别退化成只看退出码 —— demo 普遍无条件 ``return 0``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import verify_examples as vex
from scripts.verify_examples import Tier, Verdict, classify, discover

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("rel", "source", "expected"),
    [
        # SKILL.md 里挂的脚本由运行时调用,不是独立入口
        ("examples/x/skills/s/scripts/f.py", "", Tier.SKIP_SKILL_SCRIPT),
        # 下划线前缀 / _lib 后缀 = 库模块
        ("examples/_provider_bootstrap.py", "", Tier.SKIP_HELPER),
        ("examples/x/__init__.py", "", Tier.SKIP_HELPER),
        ("examples/x/hooks_lib.py", "", Tier.SKIP_HELPER),
        # 显式登记的非入口
        ("examples/step_pipeline/pipeline.py", "", Tier.SKIP_NOT_ENTRY),
        # 引用 _provider_bootstrap 即需真实 key
        ("examples/x/demo.py", "from _provider_bootstrap import x", Tier.SKIP_NEEDS_KEY),
        # 常驻服务不自行退出
        ("examples/x/server.py", "", Tier.SKIP_NOT_ENTRY),
        # 兜底:未命中任何跳过规则 → 执行(安全默认)
        ("examples/x/demo.py", "print(1)", Tier.RUN),
    ],
)
def test_classify_rules(rel: str, source: str, expected: Tier) -> None:
    """分档规则逐条覆盖,含「默认执行」的安全兜底。"""
    tier, reason = classify(rel, source)
    assert tier is expected
    assert reason  # 每档都必须给出人能读懂的理由


def test_classify_skill_script_wins_over_needs_key() -> None:
    """规则有序:skills/ 下的脚本即使含 key 标记也归 skill-script 档。"""
    tier, _ = classify(
        "examples/x/skills/s/scripts/f.py", "import _provider_bootstrap"
    )
    assert tier is Tier.SKIP_SKILL_SCRIPT


@pytest.mark.parametrize(
    ("code", "markers", "chars", "failed"),
    [
        (0, (), 100, False),           # 正常通过
        (1, (), 100, True),            # 退出码非 0
        ("TIMEOUT", (), 100, True),    # 超时
        (0, ("❌",), 100, True),        # 退出码 0 但带内失败标记 —— 核心防线
        (0, (), 0, True),              # 跑完什么都没打印
    ],
)
def test_verdict_failed_triple_judgement(
    code: object, markers: tuple[str, ...], chars: int, failed: bool
) -> None:
    """三重判定:退出码 / 硬失败标记 / 空输出,任一不满足即 FAIL。"""
    verdict = Verdict(path="p", tier=Tier.RUN, reason="", code=code,
                      markers=markers, chars=chars)
    assert verdict.failed is failed


def test_verdict_skipped_never_fails() -> None:
    """跳过档不参与判定,不会因为字段缺省被误判 FAIL。"""
    verdict = Verdict(path="p", tier=Tier.SKIP_NEEDS_KEY, reason="")
    assert verdict.failed is False


def test_discover_rejects_stale_not_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOT_ENTRY 登记了已不存在的路径要立刻报错,防条目变陈旧。"""
    monkeypatch.setattr(vex, "NOT_ENTRY", {"examples/gone/nope.py": "已删除"})
    with pytest.raises(SystemExit, match="NOT_ENTRY"):
        discover(REPO_ROOT)


def test_discover_on_real_tree() -> None:
    """真实 examples/ 树:有可执行示例,且已知文件落在预期档位。"""
    rows = discover(REPO_ROOT)
    tiers = {rel: tier for rel, tier, _ in rows}
    assert sum(1 for t in tiers.values() if t is Tier.RUN) >= 20
    assert tiers["examples/basic/minimal_chat.py"] is Tier.RUN
    assert tiers["examples/mcp_basic/demo.py"] is Tier.SKIP_NEEDS_KEY
    assert tiers["examples/real_llm/capability_matrix.py"] is Tier.SKIP_NEEDS_KEY
    assert "__pycache__" not in " ".join(tiers)


def test_hard_markers_match_real_failure_text() -> None:
    """硬失败标记必须能命中真实事故文本,否则形同虚设。"""
    samples = [
        "Traceback (most recent call last):",
        '{"kind": "turn_failed"}',
        "  ← isError=True",
        "[TURN ✗] 失败",
        "❌ 缺少 LLM_BOOTSTRAP_API_KEY",
    ]
    for text in samples:
        assert any(re.search(p, text) for p in vex.HARD_MARKERS), text
