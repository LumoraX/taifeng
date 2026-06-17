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

import math
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anyio.lowlevel

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


# ----------------------------------------------------------------------------
# 分词常量（魔法值集中声明，禁止散落字面量）
# ----------------------------------------------------------------------------

# matched_snippet 以首个命中 query 词为中心截取的片段半窗（字符数）。
# 取 20 是「看一眼能判断命中上下文」与「不把整段塞进去」之间的平衡。
_SNIPPET_HALF_WINDOW = 20

# BM25 词频饱和参数 k1：控制 term frequency 的饱和速度。tf 越大，边际增益越小，
# 命中 1→2 次涨分明显、10→11 次几乎不涨，避免「同一词刷满全篇」碾压多样命中。
# 1.5 是 BM25 社区经验默认值（典型区间 1.2–2.0）。
_BM25_K1 = 1.5

# BM25 长度归一强度 b ∈ [0,1]：0 表示完全不按文档长度归一，1 表示完全归一。
# 长文天然命中词更多，b 把长度惩罚放进**分母**做饱和（而非线性乘子），既压住长文
# 虚高、又不会无界惩罚长相关文档。0.75 是 BM25 社区经验默认值。
_BM25_B = 0.75

# CJK 表意文字判定：用 unicodedata 取字符名前缀，避免硬编码码点区间易漏。
# 注意：这里**只覆盖 CJK 统一表意文字（汉字，含中文及日文汉字）**，
# **不含**日文假名（Hiragana/Katakana）与韩文谚文（Hangul）——本相位语料以中文为主，
# 假名/谚文按 YAGNI 不纳入（如需支持，应补对应前缀并加测试，见评审 Important-1 选项 B）。
_CJK_NAME_PREFIXES = ("CJK UNIFIED", "CJK COMPATIBILITY")


def _is_cjk_char(ch: str) -> bool:
    """判定单个字符是否属于 CJK 统一表意文字（汉字，含中文及日文汉字）。

    用 ``unicodedata.name`` 的官方字符名前缀判定，比硬编码码点区间更稳健、可读。
    **不含**日文假名与韩文谚文（见模块常量 ``_CJK_NAME_PREFIXES`` 注释的范围说明）。

    Args:
        ch: 单字符字符串。

    Returns:
        True 表示该字符是 CJK 统一表意文字（汉字）。
    """
    # 无名字符（如部分控制符）直接判否；name 不存在时给空串而非抛异常
    name = unicodedata.name(ch, "")
    return any(name.startswith(prefix) for prefix in _CJK_NAME_PREFIXES)


def _tokenize(text: str) -> list[str]:
    """把文本切成检索词单元（零依赖、确定性）。

    分词策略（为何这样切，注释清楚）：
        - **ASCII / 拉丁段**：按「非字母数字」边界切词并小写。英文天然以空格 / 标点
          分词，按词切是无歧义的标准做法。
        - **CJK 连续段**：中文没有空格分词，且零依赖下不能引入 jieba 等分词器。
          折中采用**字符 bigram**（相邻两字成一词，如「报销纠纷」→「报销」「销纠」
          「纠纷」）：bigram 比单字更能区分语义（「报销」vs 单独的「报」「销」），
          又不依赖词典，是零依赖约束下召回精度与实现成本的平衡点。
          单字段（CJK 段只有一个字）退化为补该单字，避免漏召回。

    Args:
        text: 待分词文本。

    Returns:
        词单元列表（可能含重复，调用方据需要统计词频）。
    """
    tokens: list[str] = []
    # ascii_run 累积「当前正在收集的拉丁字母数字段」
    ascii_run: list[str] = []
    # cjk_run 累积「当前正在收集的 CJK 连续段」，到边界时切 bigram
    cjk_run: list[str] = []

    def _flush_ascii() -> None:
        """把累积的拉丁段作为一个小写词落出。"""
        if ascii_run:
            tokens.append("".join(ascii_run).lower())
            ascii_run.clear()

    def _flush_cjk() -> None:
        """把累积的 CJK 段切 bigram 落出；单字段补单字。"""
        if not cjk_run:
            return
        if len(cjk_run) == 1:
            # 单字段：无法成 bigram，退化补单字，避免漏召回
            tokens.append(cjk_run[0])
        else:
            # 相邻两字成一个 bigram 词
            for i in range(len(cjk_run) - 1):
                tokens.append(cjk_run[i] + cjk_run[i + 1])
        cjk_run.clear()

    for ch in text:
        if _is_cjk_char(ch):
            # 进入 CJK 字符：先结束未完的 ascii 段，再累积到 cjk 段
            _flush_ascii()
            cjk_run.append(ch)
        elif ch.isalnum() and ch.isascii():
            # 拉丁字母 / 数字：先结束未完的 cjk 段，再累积到 ascii 段
            _flush_cjk()
            ascii_run.append(ch)
        else:
            # 其它（空格 / 标点 / 非 CJK 的 Unicode）一律视为分词边界
            _flush_ascii()
            _flush_cjk()

    # 文本结束：把残留的两个段都落出
    _flush_ascii()
    _flush_cjk()
    return tokens


def _make_snippet(description: str, query_tokens: list[str]) -> str | None:
    """以首个命中 query 词为中心，截取 description 的一段作命中片段。

    片段用于审计「为何被召回」。命中词在 description 中按原始子串定位（CJK bigram
    与 ascii 词都是 description 的子串，故可直接 ``find``）。

    Args:
        description: 候选 skill 描述（原文，不分词）。
        query_tokens: query 分词后的词单元列表。

    Returns:
        命中片段字符串；若没有任何 query 词出现在 description 中则返回 None
        （禁止用空串伪装「无片段」，契约要求 None）。
    """
    # 按 query 词顺序找第一个真实出现在 description 中的词，定位其位置
    for tok in query_tokens:
        pos = description.find(tok)
        if pos >= 0:
            # 以命中位置为中心，向两侧各扩 _SNIPPET_HALF_WINDOW 字符
            start = max(0, pos - _SNIPPET_HALF_WINDOW)
            end = min(len(description), pos + len(tok) + _SNIPPET_HALF_WINDOW)
            return description[start:end]
    # 无任何 query 词出现在原文（理论上空匹配候选已被过滤，此处稳妥返回 None）
    return None


class KeywordSkillRecall:
    """内核默认召回后端：零依赖、确定性的关键词召回（BM25 风格饱和打分）。

    实现 ``SkillRecall`` 协议。打分采用**标准 BM25 公式**（idf 加权 + tf 饱和 + 长度
    饱和归一），对每个 query 词累加：

        score(d) = Σ_t  idf(t) · ( tf(t,d) · (k1+1) )
                                  / ( tf(t,d) + k1·(1 − b + b·|d|/avgdl) )

    三个核心要素（为何这样设计）：
        - **idf 权重**：对 pool 内全部 description 建文档频，稀有词（只在少数候选里
          出现）权重高、常见词权重低，避免「数据」「能力」这类高频词主导排名。
          采用 BM25 标准 idf ``log((N − df + 0.5)/(df + 0.5) + 1)``，``+1`` 保证恒正。
        - **tf 饱和**：真正用上候选内 term frequency ``tf(t,d)``（命中次数）——命中
          越多/越频繁单调加分，但经 ``k1`` 饱和后边际递减，单词刷满全篇不会无限涨分。
        - **长度饱和归一**：长度项 ``b·|d|/avgdl`` 放在**分母**（而非旧版线性乘子），
          长文天然命中多被适度折减、却**不被无界惩罚**——修正旧版「短垃圾排在长相关
          文档前」的相关性排反（评审 Critical-1）。

    确定性：分词、打分、排序全程不触系统时钟 / 随机源；同分按 ``skill_id`` 升序定序，
    不依赖 dict 迭代序（满足约束 C8）。

    参照 codex/claw-code 的「内核只定协议、默认实现可被业务替换」范式，差异：本类是
    纯标准库关键词召回，向量 / 外部检索由业务侧实现同协议替换。
    """

    def _score_pool(
        self,
        query_tokens: list[str],
        pool: Sequence[RecallEntry],
    ) -> list[float]:
        """对 pool 内每个候选算 BM25 风格饱和相关度分（idf + tf 饱和 + 长度归一）。

        Args:
            query_tokens: query 分词后的词单元列表。
            pool: 召回语料池。

        Returns:
            与 pool 等长的分数列表（一一对应，≥0）。
        """
        # 1) 预分词每个候选，统计文档频（df：含某词的候选数）
        doc_tokens: list[list[str]] = [_tokenize(e.description) for e in pool]
        df: dict[str, int] = {}
        for toks in doc_tokens:
            # 同一候选内同词只计一次文档频
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1

        # 2) 计算平均文档长度 avgdl（BM25 长度归一基准），空池在调用前已拦截，len≥1
        total_len = sum(len(toks) for toks in doc_tokens)
        avg_len = total_len / len(pool) if total_len > 0 else 1.0

        n_docs = len(pool)
        # query 去重词集：同一 query 词只累加一次该词的 BM25 贡献（query 内重复不刷分），
        # 但**文档内**该词的 tf 仍按真实命中次数参与饱和。
        query_set = set(query_tokens)

        scores: list[float] = []
        for toks in doc_tokens:
            # 候选内词频表 tf(t,d)：真正用上 term frequency 喂给 BM25 饱和（Minor-1）
            term_freq: dict[str, int] = {}
            for tok in toks:
                term_freq[tok] = term_freq.get(tok, 0) + 1

            # 文档长度 |d|；空文档兜底为 1 避免 avgdl 比例异常（理论上不会空）
            doc_len = len(toks) if toks else 1
            score = 0.0
            for q in query_set:
                tf = term_freq.get(q, 0)
                if tf == 0:
                    # 未命中的 query 词不贡献分（含池中不存在词）
                    continue
                # BM25 标准 idf：稀有词权重高，``+1`` 保证恒正（df==N 时也不为负）
                idf = math.log((n_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5) + 1.0)
                # 长度饱和因子：长文 → 分母增大 → 单词命中收益被适度折减（不无界）
                length_factor = 1.0 - _BM25_B + _BM25_B * doc_len / avg_len
                # BM25 tf 饱和项：tf 越大边际增益越小
                saturated_tf = tf * (_BM25_K1 + 1.0) / (tf + _BM25_K1 * length_factor)
                score += idf * saturated_tf
            scores.append(score)
        return scores

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        """据 query 在 pool 内召回 top_k 个最相关候选（实现 SkillRecall 协议）。

        流程：分词 query → 池内 idf+长度归一打分 → 过滤空匹配 → 同池归一 confidence
        → confidence 降序 / 同分 skill_id 升序 稳定排序 → top_k 截断。

        Args:
            query: 召回查询。
            pool: 召回语料池（内核已按白名单封闭）。
            top_k: 返回候选数上界。
            cancel: 级联取消 token；入口与打分后各 check 一次，尽早中断。

        Returns:
            满足契约全部约束的候选列表（降序）。

        Raises:
            asyncio.CancelledError: cancel 已取消时由 ``raise_if_cancelled`` 抛出。
        """
        # 入口先让出一次调度点并检查取消，避免已取消仍空跑
        await anyio.lowlevel.checkpoint()
        cancel.raise_if_cancelled()

        # 空池直接返回空列表（无需打分；同时规避后续除零）
        if not pool:
            return []

        query_tokens = _tokenize(query)
        # query 无有效词（如纯标点）→ 不可能命中，返回空
        if not query_tokens:
            return []

        # 池内打分（idf 加权 + 长度归一）
        scores = self._score_pool(query_tokens, pool)
        # 打分是潜在大循环之后，再检查一次取消
        cancel.raise_if_cancelled()

        # C7：非空池但全 0 匹配 → max 为 0，直接返回空列表（避免除零归一）
        max_score = max(scores)
        if max_score <= 0.0:
            return []

        # 组装候选：空匹配（score==0）的候选不入选；confidence 同池归一到 [0,1]
        candidates: list[SkillCandidate] = []
        for entry, score in zip(pool, scores, strict=True):
            if score <= 0.0:
                # 空匹配候选直接跳过（契约：score==0 不入选）
                continue
            candidates.append(
                SkillCandidate(
                    skill_id=entry.skill_id,
                    description=entry.description,
                    score=score,
                    confidence=score / max_score,
                    matched_snippet=_make_snippet(entry.description, query_tokens),
                )
            )

        # 稳定排序：confidence 降序为主；同 confidence 按 skill_id 升序（C8 确定性）。
        # 用 (-confidence, skill_id) 作 key，Python sort 稳定且不依赖 dict 迭代序。
        candidates.sort(key=lambda c: (-c.confidence, c.skill_id))

        # top_k 截断（top_k 可能 ≥ 候选数，切片天然安全）
        return candidates[:top_k]
