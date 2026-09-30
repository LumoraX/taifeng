"""skill 来源信任分层与按目录指定来源（ADR 0090）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.skill import SkillDefinition
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.skill.trust import SkillTrustPolicy, SourceTrustPolicy, tier_of

_SKILL = """---
name: {name}
description: {name} 处理具体子任务
version: 1.0.0
type: atomic
---
# {name}
"""


def _skill(skill_id: str, source: str = "user") -> SkillDefinition:
    return SkillDefinition(
        id=skill_id, name=skill_id, description="d", version="1", body="",
        body_path=Path("/nonexistent") / skill_id / "SKILL.md", type="atomic",
        source=source,  # type: ignore[arg-type]
    )


def _write(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(_SKILL.format(name=name), encoding="utf-8")


def test_default_tiers_follow_the_source() -> None:
    policy = SourceTrustPolicy()

    assert policy.tier(_skill("a", "system")) == "trusted"
    assert policy.tier(_skill("b", "user")) == "standard"
    assert policy.tier(_skill("c", "marketplace")) == "untrusted"
    assert isinstance(policy, SkillTrustPolicy)


def test_override_wins_over_source() -> None:
    policy = SourceTrustPolicy(overrides={"vetted": "trusted"})

    assert policy.tier(_skill("vetted", "marketplace")) == "trusted"
    assert policy.tier(_skill("other", "marketplace")) == "untrusted"


def test_custom_source_mapping() -> None:
    policy = SourceTrustPolicy(by_source={
        "system": "trusted", "user": "trusted", "marketplace": "standard",
    })

    assert policy.tier(_skill("a", "user")) == "trusted"
    assert policy.tier(_skill("b", "marketplace")) == "standard"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"by_source": {"system": "trusted", "user": "standard", "marketplace": "gold"}},
        {"overrides": {"x": "gold"}},
        {"by_source": {"system": "trusted"}},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        SourceTrustPolicy(**kwargs)  # type: ignore[arg-type]


def test_tier_is_unknown_without_a_policy() -> None:
    assert tier_of(None, _skill("a", "marketplace")) is None
    assert tier_of(SourceTrustPolicy(), _skill("a", "marketplace")) == "untrusted"


async def test_registry_assigns_source_by_directory(tmp_path: Path) -> None:
    own, market = tmp_path / "own", tmp_path / "market"
    _write(own, "alpha")
    _write(market, "beta")

    registry = await FilesystemSkillRegistry.load(
        [own, market], sources={market: "marketplace"}
    )

    snapshot = registry.snapshot()
    alpha, beta = snapshot.get("alpha"), snapshot.get("beta")
    assert alpha is not None and beta is not None
    assert (alpha.source, beta.source) == ("user", "marketplace")


async def test_registry_defaults_to_user_source(tmp_path: Path) -> None:
    _write(tmp_path / "skills", "alpha")

    registry = await FilesystemSkillRegistry.load(tmp_path / "skills")

    alpha = registry.snapshot().get("alpha")
    assert alpha is not None
    assert alpha.source == "user"


def test_registry_rejects_unknown_directory_and_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not loaded"):
        FilesystemSkillRegistry([tmp_path / "a"], sources={tmp_path / "b": "system"})
    with pytest.raises(ValueError, match="unknown skill source"):
        FilesystemSkillRegistry(
            [tmp_path / "a"], sources={tmp_path / "a": "vendor"},  # type: ignore[dict-item]
        )
