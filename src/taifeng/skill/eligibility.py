"""G4a：skill 运行时资格判定（纯比对，不读任何运行时状态）。

参照 openclaw src/agents/skills/config.ts::shouldIncludeSkill。

R1 守则：``src/`` 内**禁止** os.getenv / PATH 探测。运行时能力（哪些 bin 在
PATH、哪些 env 已设、当前 OS）由**业务侧**采集后通过 ``RuntimeCapabilities``
注入；本模块只做集合包含比对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.skill.definition import SkillDefinition

if TYPE_CHECKING:
    from taifeng.llm.client import ModelCapabilities


@dataclass(frozen=True)
class RuntimeCapabilities:
    """业务侧注入的运行时能力快照。"""

    available_bins: frozenset[str] = frozenset()
    """PATH 上存在的可执行文件名集合（业务用 shutil.which 等采集）。"""
    present_env: frozenset[str] = frozenset()
    """已设置的环境变量名集合（只传名字，不传值）。"""
    os_name: str = ""
    """当前 OS（``linux`` / ``darwin`` / ``windows`` 等；业务用 sys.platform 映射）。"""
    modalities: frozenset[str] = frozenset()
    """当前可用的模型模态能力标签。

    与其余三项不同：这些标签**由内核**从注入的 ModelClient 自己声明的
    ``ModelCapabilities`` 派生（``derive_modality_tags``），并在
    ``build_api_request`` 里与业务传入的本字段取并集。业务仍可补充自定义标签。
    """


def derive_modality_tags(capabilities: ModelCapabilities) -> frozenset[str]:
    """从 client 自己声明的 ``ModelCapabilities`` 派生模态标签。

    标签词表（内核拥有，与 ``SkillRequirements.modalities`` 的声明面一一对应）：
        - ``text``：恒有。
        - ``input_image``：user 消息能承载图片。
        - ``tool_output_image``：``function_call_output`` 能承载图片。

    R1：只读注入对象**自己的声明**，不做任何模型名 / 域名推断——这与「任何
    客户端都不能根据模型名或域名自动打开图片能力」是同一条规矩。

    Args:
        capabilities: 某个 ModelClient 声明的能力。

    Returns:
        可直接并入 ``RuntimeCapabilities.modalities`` 的标签集合。
    """
    tags = {"text"}
    if "image" in capabilities.input_modalities:
        tags.add("input_image")
    if "image" in capabilities.tool_output_modalities:
        tags.add("tool_output_image")
    return frozenset(tags)


def is_skill_eligible(
    skill: SkillDefinition, capabilities: RuntimeCapabilities
) -> bool:
    """判定 skill 的 ``requires`` 是否被 capabilities 满足。

    空要求恒为 True。任一未满足 → False。
    """
    req = skill.requires
    if req.is_empty():
        return True
    if req.bins and not req.bins <= capabilities.available_bins:
        return False
    if req.env and not req.env <= capabilities.present_env:
        return False
    if req.modalities and not req.modalities <= capabilities.modalities:
        return False
    return not (req.os and capabilities.os_name not in req.os)
