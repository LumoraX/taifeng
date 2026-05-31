"""G4a：skill 运行时资格判定（纯比对，不读任何运行时状态）。

参照 openclaw src/agents/skills/config.ts::shouldIncludeSkill。

R1 守则：``src/`` 内**禁止** os.getenv / PATH 探测。运行时能力（哪些 bin 在
PATH、哪些 env 已设、当前 OS）由**业务侧**采集后通过 ``RuntimeCapabilities``
注入；本模块只做集合包含比对。
"""

from __future__ import annotations

from dataclasses import dataclass

from taifeng.skill.definition import SkillDefinition


@dataclass(frozen=True)
class RuntimeCapabilities:
    """业务侧注入的运行时能力快照。"""

    available_bins: frozenset[str] = frozenset()
    """PATH 上存在的可执行文件名集合（业务用 shutil.which 等采集）。"""
    present_env: frozenset[str] = frozenset()
    """已设置的环境变量名集合（只传名字，不传值）。"""
    os_name: str = ""
    """当前 OS（``linux`` / ``darwin`` / ``windows`` 等；业务用 sys.platform 映射）。"""


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
    return not (req.os and capabilities.os_name not in req.os)
