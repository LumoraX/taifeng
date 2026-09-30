"""skill 来源信任分层 —— 三层防假里的「供给」一层（skill-working-set，ADR 0090）。

选择置信度看长相，战绩看结果，来源信任看这个 skill 是谁放进来的。三者互不替代：

- 长相可以被精心写的描述骗；
- 战绩要跑过才有，新 skill 没有战绩；
- 来源在加载时就确定，模型与 skill 自己都改不了。

信任层级只由**加载位置**（``SkillDefinition.source``）与业务的显式指定决定，不读 SKILL.md 里的
任何自述字段：自己声明自己可信没有意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, get_args, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from taifeng.skill.definition import SkillDefinition, SkillSource

TrustTier = Literal["trusted", "standard", "untrusted"]
"""来源信任层级，从高到低。"""

TRUST_TIERS: tuple[TrustTier, ...] = get_args(TrustTier)

def _default_by_source() -> dict[SkillSource, TrustTier]:
    """缺省分层：内置的可信、业务自己的标准、外部获取的不可信。"""
    return {"system": "trusted", "user": "standard", "marketplace": "untrusted"}


@runtime_checkable
class SkillTrustPolicy(Protocol):
    """来源信任分层协议：内核给默认实现，业务可注入自己的口径。"""

    def tier(self, skill: SkillDefinition) -> TrustTier:
        """该 skill 的信任层级。SHALL 是无副作用的同步判断，同一 skill 恒得同一结果。"""
        ...


def _check_tiers(entries: Iterable[tuple[str, str]], what: str) -> None:
    """校验层级取值；非法值显式报错。"""
    for key, value in entries:
        if value not in TRUST_TIERS:
            raise ValueError(f"unknown trust tier {value!r} for {what} {key!r}")


@dataclass(frozen=True)
class SourceTrustPolicy:
    """默认分层：按加载来源，个别 skill 可单独指定。

    Attributes:
        by_source: 来源 → 层级。缺省 ``system`` 可信、``user`` 标准、``marketplace`` 不可信。
        overrides: skill id → 层级，优先于来源。

    Raises:
        ValueError: 层级取值非法，或 ``by_source`` 没有覆盖某个来源。
    """

    by_source: Mapping[SkillSource, TrustTier] = field(default_factory=_default_by_source)
    overrides: Mapping[str, TrustTier] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """构造期校验。"""
        _check_tiers(self.by_source.items(), "source")
        _check_tiers(self.overrides.items(), "skill")
        missing = sorted(set(_default_by_source()) - set(self.by_source))
        if missing:
            raise ValueError(f"by_source does not cover sources: {missing}")

    def tier(self, skill: SkillDefinition) -> TrustTier:
        """单独指定优先，否则按来源。"""
        override = self.overrides.get(skill.id)
        return override if override is not None else self.by_source[skill.source]


def tier_of(policy: SkillTrustPolicy | None, skill: SkillDefinition) -> TrustTier | None:
    """未配置信任策略时层级未知（None），不假定任何层级。"""
    return None if policy is None else policy.tier(skill)


__all__ = [
    "TRUST_TIERS",
    "SkillTrustPolicy",
    "SourceTrustPolicy",
    "TrustTier",
    "tier_of",
]
