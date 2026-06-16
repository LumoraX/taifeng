"""skill 召回（发现）契约层 —— 相位 2「skill 发现/召回」的数据地基。

定位：当白名单内 skill 数量膨胀到「装不进一次 prompt」时，需要先据 query 召回
top_k 个最相关候选再交给 LLM。本模块只定义**契约**（数据结构 + 协议），不绑定任何
具体召回算法：

- ``SkillCandidate`` —— 一条召回候选（透给 LLM 的展示项 + 审计依据）。
- ``RecallEntry`` —— 召回语料池里的一项（内核按 caller 白名单解析后喂给后端）。
- ``SkillRecall`` —— 可插拔召回后端协议（关键词 / 向量 / 外部检索服务均可实现）。

下游：T2 的 KeywordSkillRecall、T5 的 search_skills 工具均依赖本契约。

R1 业务零侵入：本模块不含任何业务概念，纯通用 skill 发现原语。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.loop.cancellation import CancellationToken


@dataclass(frozen=True)
class SkillCandidate:
    """一条召回候选：把「后端原始相关度」与「归一化置信」分开存。

    字段语义：
        skill_id: 候选 skill 的 id（必须 ⊆ 召回时传入的 pool）。
        description: 候选 skill 的描述（透给 LLM 做二次决策）。
        score: 后端原始相关度，≥0，**仅同次召回内可比**（不同后端 / 不同 query
            之间无可比性，禁止跨次比较或持久化做阈值）。
        confidence: 把 score 归一化到 [0,1] 的相对置信。
            注意：**本相位仅作数据透给 LLM、不消费 / 不分流**——是否据 confidence
            自动放行 / 拦截 / 降级属后续相位的分流策略，本契约不承诺任何此类语义。
        matched_snippet: 命中片段（便于看「为何被召回」+ 审计追溯）；无则 None，
            禁止用空串伪装「无片段」。
    """

    skill_id: str
    description: str
    score: float
    confidence: float
    matched_snippet: str | None


@dataclass(frozen=True)
class RecallEntry:
    """召回语料池里的一项。

    内核按 caller 白名单解析出可见 skill 集合后，把每个 skill 包成 RecallEntry 传给
    后端；后端只能在这个池内排名。这样「召回限定在白名单内」这条安全约束被**钉在内核
    手里**，召回后端无从越权召回白名单外的 skill。

    字段语义：
        skill_id: skill 的 id。
        description: skill 描述（召回后端据此做相关性匹配）。
    """

    skill_id: str
    description: str


@runtime_checkable
class SkillRecall(Protocol):
    """可插拔召回后端协议：据 query 在 pool 内排名，返回 top_k 候选。

    实现方可以是关键词匹配（T2）、向量检索、或外部检索服务；内核只依赖本协议。

    约束（实现方必须满足）：
        - **白名单封闭**：返回的每个 ``SkillCandidate.skill_id`` 必须 ⊆ ``pool`` 内的
          skill_id 集合，禁止凭空召回池外 skill。
        - **数量上界**：``len(返回列表) ≤ top_k``。
        - **置信合法**：每个候选的 ``confidence ∈ [0, 1]``。
        - **纯函数 / 确定性**：相同 (query, pool, top_k) 必须给出相同结果；**禁止使用
          系统时钟 / 随机源**（确定性内核要求，便于 replay 与测试）。
        - **可取消**：收到 ``cancel`` 取消信号应能尽早中断，不阻塞主 actor。
    """

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        """据 query 在 pool 内召回 top_k 个最相关候选。

        Args:
            query: 召回查询（通常是用户意图 / 当前任务描述）。
            pool: 召回语料池（内核已按白名单封闭，后端只在此池内排名）。
            top_k: 返回候选数上界。
            cancel: 级联取消 token，收到取消应尽早中断。

        Returns:
            按相关度降序的候选列表，满足上述全部约束。
        """
        ...
