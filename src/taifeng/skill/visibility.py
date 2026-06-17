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

from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot


def effective_child_recall(
    entry: SkillDefinition, *, child_count: int, threshold: int
) -> Literal["inline", "deferred"]:
    """裁定 entry 的 child skill 生效召回模式（``inline`` 还是 ``deferred``）。

    这是 **prompt 文本构建** 与 **per-turn 工具裁剪** 共用的**唯一判定**——两侧
    都调本函数，保证「prompt 是否 inline 列 child」与「是否暴露 ``search_skills``
    工具」严格一致（否则会出现「prompt 说没列、工具却不给搜」或反之的撕裂）。

    判定规则（基于 ``entry.exposure.child_recall`` 三值声明）：
    - ``"inline"`` → 强制 ``"inline"``（无论 child 多少，全部内联列出）。
    - ``"deferred"`` → 强制 ``"deferred"``（无论 child 多少，都走 search_skills 召回）。
    - ``"auto"`` → 据 ``child_count`` 与 ``threshold`` 比较：``> threshold`` 时
      ``"deferred"``（child 太多、装不进一次 prompt），否则 ``"inline"``。

    ``child_count`` 应传 **G4 过滤后的可见 child 数**（见 ``visible_child_skills``），
    与实际会内联 / 召回的池规模一致，而非声明的原始 ``child_skills`` 总数。

    R2 cache 声明：本判定决定 entry **静态 system prompt 形状**（pre-turn 决定、
    整 turn 稳定），**不是 mid-turn cache 失效**；不返回 ``CompressionResult``。
    inline / deferred 导致的 cache key 差异是每 skill 的静态属性（同一 entry 跨 turn
    走同一分支 → prefix 稳定）。

    Args:
        entry: 当前 caller composite skill（提供 ``exposure.child_recall`` 声明）。
        child_count: G4 过滤后的可见 child 数（``auto`` 模式下与 threshold 比较）。
        threshold: ``auto`` 模式下切到 deferred 的 child 数阈值（业务可配，DI 注入）。

    Returns:
        ``"inline"`` 或 ``"deferred"``——生效的召回模式。
    """
    mode = entry.exposure.child_recall
    # 显式声明优先：inline / deferred 直接锁定，不看 child 数
    if mode == "inline":
        return "inline"
    if mode == "deferred":
        return "deferred"
    # auto：child 数超阈值 → deferred（装不进一次 prompt），否则 inline
    return "deferred" if child_count > threshold else "inline"


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
