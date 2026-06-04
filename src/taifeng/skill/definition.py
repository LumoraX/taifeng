"""SkillDefinition —— 统一 skill 描述符。

按 ADR 0006，原子 / 组合通过 ``type`` 字段区分；不存在 Agent 抽象。

参照：docs/architecture/skill-system.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from taifeng.skill.orchestration import OrchestrationSpec
    from taifeng.skill.scripts.types import ScriptDescriptor

SkillType = Literal["atomic", "composite"]
SkillSource = Literal["system", "user", "marketplace"]


class SkillValidationError(ValueError):
    """skill 自校验失败。"""


@dataclass(frozen=True)
class SkillRequirements:
    """G4a：skill 运行时资格门控声明（来自 frontmatter ``requires``）。

    任一非空集合即视为一项硬要求；不满足的 skill 不会出现在 prompt 的
    ``available_child_skills`` 列表里（避免浪费 context + 必失败的派发）。

    R1 注意：本类只声明「需要什么」，**不读取**任何运行时状态。实际是否满足
    由业务侧注入的 ``RuntimeCapabilities`` 比对（src/ 内禁止 os.getenv / PATH 探测）。
    """

    bins: frozenset[str] = frozenset()
    """需要 PATH 上存在的可执行文件名（如 ``jq`` / ``rg``）。"""
    env: frozenset[str] = frozenset()
    """需要已设置的环境变量名（只看「是否存在」，不读值）。"""
    os: frozenset[str] = frozenset()
    """需要的 OS（``linux`` / ``darwin`` / ``windows``）；空=任意。"""

    def is_empty(self) -> bool:
        return not (self.bins or self.env or self.os)


@dataclass(frozen=True)
class SkillExposure:
    """G4b：skill 曝光维度拆分。

    把"对模型可见"与"对用户可见"解耦，支持「用户可显式触发、但对模型隐藏」
    （slash-command 风格）或「注册但不进 prompt 列表」等组合。
    """

    model_invocable: bool = True
    """是否出现在 ``available_child_skills`` 列表（供 LLM ``call_skill``）。"""
    user_invocable: bool = True
    """是否允许业务侧/用户显式触发（taifeng 不解析，仅原样携带供业务判断）。"""


@dataclass(frozen=True)
class SkillDefinition:
    """统一 skill 描述符。

    Atomic 与 composite 通过 ``type`` 字段区分。Composite 独有字段
    （``child_skills`` / ``tool_names`` / ``max_call_depth`` / ``model``）
    在 atomic 上必须留空；composite 则需 ``child_skills`` 或 ``tool_names``
    至少其一非空（tool-only composite 合法，见 ADR 0013），均由 ``validate()`` 强制。
    """

    # === 通用字段 ===
    id: str
    name: str
    description: str
    version: str
    body: str
    body_path: Path

    # === 分层标记 ===
    type: SkillType
    entry: bool = False

    # === Composite 特有字段（atomic 必须全部留空）===
    child_skills: frozenset[str] = frozenset()
    tool_names: frozenset[str] = frozenset()
    max_call_depth: int = 6
    model: str | None = None

    # === B 声明式编排（仅 composite 可声明；atomic 声明即报错）===
    orchestration: OrchestrationSpec | None = None

    # === G4 可见性治理（atomic / composite 通用）===
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    exposure: SkillExposure = field(default_factory=SkillExposure)

    # === 业务透传 ===
    frontmatter_raw: dict = field(default_factory=dict)
    scripts: tuple[ScriptDescriptor, ...] = ()
    source: SkillSource = "user"

    def validate(self) -> None:
        """启动期约束校验。失败立即抛 ``SkillValidationError``。"""
        if self.type == "atomic":
            if self.child_skills:
                raise SkillValidationError(
                    f"atomic skill {self.id!r} 不能声明 child_skills"
                )
            if self.tool_names:
                raise SkillValidationError(
                    f"atomic skill {self.id!r} 不能声明 tool_names"
                )
            if self.entry:
                raise SkillValidationError(
                    f"atomic skill {self.id!r} 默认不可作为 entry"
                )
            if self.model is not None:
                raise SkillValidationError(
                    f"atomic skill {self.id!r} 不应声明 model 偏好"
                )
            if self.orchestration is not None:
                raise SkillValidationError(
                    f"atomic skill {self.id!r} 不能声明 orchestration"
                )
        elif self.type == "composite":
            # composite = 有 agency 的 skill：可调子 skill、可调工具，二者至少其一。
            # 两者皆空 = 戴帽子的 atomic（无意义空壳）→ fail-fast 拒绝。
            # （参照 ADR 0013：放松"必须有 child_skills"为"二者至少其一"。）
            if not self.child_skills and not self.tool_names:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须至少声明 child_skills 或 tool_names 之一"
                )
            if self.max_call_depth < 1:
                raise SkillValidationError(
                    f"composite skill {self.id!r} max_call_depth 必须 >= 1"
                )
        else:
            raise SkillValidationError(
                f"skill {self.id!r} 未知 type: {self.type!r}"
            )
