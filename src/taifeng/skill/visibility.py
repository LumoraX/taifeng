"""G4 子 skill 可见性过滤 —— inline 列表与 deferred 召回池的**单一真相**。

定位：caller composite skill 把它的 ``child_skills`` 暴露给 LLM 时，要施加两层
G4 可见性治理过滤：

- **G4b**：``exposure.model_invocable == False`` 的子 skill 对模型隐藏，不进列表。
- **G4a**：提供 ``RuntimeCapabilities`` 时，``requires`` 不被满足的子 skill 不进列表
  （缺二进制 / 缺环境变量 / OS 不符）。

为什么要抽这个 helper（**消除双实现漂移**）：
- ``loop/prompt.py`` 的 ``render_system_prompt`` 把 child 内联（inline）列进
  ``<available_child_skills>`` 时用这套过滤；
- ``tool/builtins/search_skills.py`` 的 deferred 召回池（子 skill 太多、装不进一次
  prompt 时改为 ``search_skills`` 按需召回）必须用**同一套**过滤，否则 deferred 路径
  会成为 G4 旁路——LLM 通过召回拿到本应被隐藏 / 不满足 requires 的 skill（C2 红线）。

把「caller.child_skills + G4 过滤 → 候选 (id, description) 列表」收敛在本函数，inline
与 deferred 两条路径**同源同过滤**，是治理一致性的结构性保证。

R1 业务零侵入：纯通用 skill 可见性原语，不含任何业务概念。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot


class VisibleChild(NamedTuple):
    """一条通过 G4 过滤的可见子 skill（id + description）。

    字段语义：
        skill_id: 子 skill 的 id。
        description: 子 skill 的描述（inline 列表展示 / deferred 召回匹配语料）。
    """

    skill_id: str
    description: str


def visible_child_skills(
    entry: SkillDefinition,
    snapshot: SkillSnapshot,
    capabilities: RuntimeCapabilities | None = None,
) -> list[VisibleChild]:
    """解析 caller entry 的 ``child_skills`` 并施加 G4 过滤，返回可见候选列表。

    遍历 ``sorted(entry.child_skills)``（按 id 升序，保证确定性），对每个 child：

    1. 在 snapshot 内解析；解析不到（注册表里没有）直接跳过。
    2. **G4b**：``exposure.model_invocable == False`` → 对模型隐藏，跳过。
    3. **G4a**：提供 ``capabilities`` 时，``is_skill_eligible`` 不满足 → 跳过。

    与 ``loop/prompt.py::render_system_prompt`` 的 inline 列表过滤逐条对齐——本函数即
    那段过滤逻辑的**唯一实现**，供 inline 与 deferred 召回池复用（避免双实现漂移）。

    Args:
        entry: 当前 caller composite skill（提供 ``child_skills`` 白名单）。
        snapshot: skill 注册表快照（据 id 解析子 skill 定义）。
        capabilities: 运行时能力快照；``None`` 时跳过 G4a（与 prompt.py 一致）。

    Returns:
        通过 G4 过滤的 ``VisibleChild`` 列表，按 ``skill_id`` 升序（确定性）。
    """
    # 延迟 import 避免与 eligibility 形成 import 期循环依赖
    from taifeng.skill.eligibility import is_skill_eligible

    visible: list[VisibleChild] = []
    # 按 id 升序遍历 child 白名单：确定性输出，且与 prompt.py 的 sorted 一致
    for child_id in sorted(entry.child_skills):
        child = snapshot.get(child_id)
        if child is None:
            # 注册表里没有该 child（声明了但未加载）：跳过，不凭空构造候选
            continue
        # G4b：对模型隐藏的 skill 不进候选（inline 与 deferred 一致）
        if not child.exposure.model_invocable:
            continue
        # G4a：提供运行时能力快照时，过滤 requires 不满足的 skill
        if capabilities is not None and not is_skill_eligible(child, capabilities):
            continue
        visible.append(
            VisibleChild(skill_id=child.id, description=child.description)
        )
    return visible
