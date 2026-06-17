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
ChildRecall = Literal["inline", "deferred", "auto"]
"""child skill 召回模式（相位 2 deferred 暴露）：

- ``inline``：把 child 列表全塞进 prompt（今天的默认行为）。
- ``deferred``：child 不塞进 prompt，改走 ``search_skills`` 检索后按需召回。
- ``auto``：按 child 数量阈值自动在 inline / deferred 间决定（阈值逻辑由
  prompt 构建侧消费，本层只承载声明）。
"""


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
    child_recall: ChildRecall = "auto"
    """该 entry 的 child skill 召回模式（``inline`` / ``deferred`` / ``auto``）。

    默认 ``auto``：由 prompt 构建侧按 child 数量阈值决定是否 deferred 暴露。
    本层只承载声明，不实现阈值判定（见 ``ChildRecall`` 三值语义）。
    """


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

    def visible_tool_names(self) -> frozenset[str]:
        """本 skill 作 entry 时的可见工具名集 —— 「声明↔可见」的单一真相。

        组成（tool-whitelist 契约）：
        - ``tool_names`` 显式声明（atomic 恒为空）；
        - ``read_skill`` / ``call_skill`` 内核恒备（skill-as-context 范式基座）；
        - ``scripts`` 非空 → 自动并入 ``run_script``——声明脚本即可见，
          atomic / composite 一致（修复「atomic + scripts 结构性不可用」）。

        消费点（如 turn 请求组装）MUST 经本函数派生，不得内联重复集合逻辑；
        registry 未注册项由消费点过滤（可见集只管「声明层应可见什么」）。
        """
        auto = frozenset({"run_script"}) if self.scripts else frozenset()
        return self.tool_names | {"read_skill", "call_skill"} | auto

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
            # composite = 有 agency 的 skill：可调子 skill、可调工具、可跑脚本，
            # 三者至少其一。全空 = 戴帽子的 atomic（无意义空壳）→ fail-fast 拒绝。
            # （参照 ADR 0013 放松"必须有 child_skills"；tool-whitelist 变更后
            # scripts 自动并入 run_script 可见集，scripts-only composite 同样有 agency。）
            if not self.child_skills and not self.tool_names and not self.scripts:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须至少声明 "
                    "child_skills / tool_names / scripts 之一"
                )
            if self.max_call_depth < 1:
                raise SkillValidationError(
                    f"composite skill {self.id!r} max_call_depth 必须 >= 1"
                )
        else:
            raise SkillValidationError(
                f"skill {self.id!r} 未知 type: {self.type!r}"
            )
