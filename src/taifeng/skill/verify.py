"""skill 验证（适配判定）契约层 —— skill 发现回路的「精验」一道闸。

定位：召回（``recall.py``）只看 ``description``（**长相**，浅）据语义粗筛候选；但
「描述像」≠「就当前任务真能用」——很多误召是「描述像但实际要的输入给不了」。本模块在
召回之后加一道**验证**：拉**完整 SKILL.md body**，用 LLM 判「就当前任务的输入/条件，
这个能力**声明要的输入是否满足、前提是否具备**」（判**适配**，不判 skill 能否跑通），
把输入要求满足不了的误召滤掉。

- ``VerifiedCandidate`` —— 验证后的候选（在召回基础上加「适配结论」）。
- ``SkillVerifier`` —— 可插拔验证后端协议（默认实现走 LLM；业务可换规则/向量验证）。
- ``LlmSkillVerifier`` —— 内核默认实现：起一次性子 LLM 调用批量判适配。

设计沿用 ``recall.py`` 的「契约 + 协议 + LLM 默认实现」三段范式（``_call_llm`` 流式累积、
``_parse_answer`` 整体失败抛错/单项脏丢弃）。

**长相与适配分离**（防呆原则）：``recall_confidence``=召回阶段相关度（长相），
``verify_confidence``=验证阶段适配置信，**两者语义不同、不可混用**——延续召回侧
「后端原始分 vs 归一化置信」分离的同一防呆思路。

R1 业务零侵入：本模块不含任何业务概念，纯通用 skill 发现原语；不读环境变量、不绑定
provider，``ModelClient`` 由业务侧依赖注入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anyio.lowlevel

from taifeng.llm.types import ApiMessage, ApiRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from taifeng.llm.client import ModelClient
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.recall import SkillCandidate


@dataclass(frozen=True)
class VerifiedCandidate:
    """验证后的候选：在召回基础上加「输入要求是否满足」的结论。

    字段语义（**长相与适配分离**，防呆）：
        skill_id: 候选 skill 的 id（必 ⊆ 入参 candidates）。
        description: 候选描述（取自入参 candidate，不信模型复述）。
        recall_confidence: **召回阶段**相关度（长相，浅）——来自 ``SkillCandidate``。
        applicable: 就当前任务输入，是否满足该能力**声明要的输入 / 前提**。
        verify_confidence: **验证阶段**适配置信 ∈ [0,1]（与长相分离，独立字段）。
        reason: 为何适用 / 不适用（便于主 LLM 二次决策 + 审计追溯）；body 被截断 /
            取不到等情形会在此注明。
    """

    skill_id: str
    description: str
    recall_confidence: float
    applicable: bool
    verify_confidence: float
    reason: str


@runtime_checkable
class SkillVerifier(Protocol):
    """可插拔验证后端协议：逐候选据完整 body 判输入要求适配。

    实现方可以是 LLM 判定（默认 ``LlmSkillVerifier``）、规则引擎、或外部服务；内核只
    依赖本协议。

    约束（实现方必须满足）：
        - **候选封闭**：返回的每个 ``VerifiedCandidate.skill_id`` 必 ⊆ ``candidates``
          里的 skill_id 集合，禁止凭空产出候选外 skill。
        - **只返回适用**：返回列表**仅含 applicable=True** 的候选（不适用的滤掉）。
        - **置信合法**：每个候选的 ``verify_confidence ∈ [0, 1]``。
        - **body 取道唯一**：完整 body 一律经 ``get_body`` 取（按 skill_id），实现方
          **不得**自取 registry snapshot——把「取哪些 body」的控制权钉在调用方手里。
        - **可取消**：收到 ``cancel`` 取消信号应尽早中断，不阻塞主 actor（R4）。
    """

    async def verify(
        self,
        task: str,
        candidates: Sequence[SkillCandidate],
        *,
        get_body: Callable[[str], str | None],
        cancel: CancellationToken,
    ) -> list[VerifiedCandidate]:
        """逐候选据完整 SKILL.md body 判输入要求适配，返回**仅 applicable=True** 的。

        Args:
            task: 当前任务 / 意图（= search 的 query）。
            candidates: 召回回来的候选（仅 description 级，未含 body）。
            get_body: 按 skill_id 取完整 SKILL.md body 的回调；取不到返回 None。
            cancel: 级联取消 token，收到取消应尽早中断。

        Returns:
            仅含 applicable=True 的验证候选列表，满足上述全部约束。
        """
        ...


# ----------------------------------------------------------------------------
# 魔法值集中声明（禁散落字面量）
# ----------------------------------------------------------------------------

# verify_confidence 合法区间下 / 上界。模型给越界值（负数 / >1）按「数据脏」丢弃该项，
# 不裁剪伪造（遵循「禁 silent fallback」红线）。
_CONFIDENCE_MIN = 0.0
_CONFIDENCE_MAX = 1.0

# 默认精验候选数上界：召回宽筛（top_k 可几十），验证只对前 N 个精验——控成本 / prompt
# 体积（C2 护栏）。取 5 是「覆盖召回头部高相关候选」与「一次 prompt 装得下完整 body」
# 之间的平衡点。
_DEFAULT_VERIFY_MAX_CANDIDATES = 5

# 默认单 body 入 prompt 的字符上限：超出截断（截断标记注入 reason）。取 4000 字符
# 约对应数千 token，保证多个候选 body 仍能一起进一次 prompt（C2 护栏）。
_DEFAULT_VERIFY_BODY_CHAR_LIMIT = 4000

# body 截断后追加到该候选 reason 的提示标记（审计可见「这条结论基于截断 body」）。
_BODY_TRUNCATED_NOTE = "（注意：body 已截断，结论基于前缀）"

# LlmSkillVerifier 系统指令（中文、要求只输出可解析 JSON、判适配不判能否跑通）。
# 为何这样写：模型一旦夹带散文，json.loads 失败 → 走显式 SkillVerifyParseError，
# 不静默伪造（遵循「禁 silent fallback、脏数据走显式错误」原则）。
_LLM_VERIFY_SYSTEM_PROMPT_ZH = (
    "你是一个 skill 适配验证器。给定一个任务和一组候选 skill（每个含 skill_id 与"
    "其完整 SKILL.md 正文）。请逐个判断：**就这个任务当前能提供的输入 / 条件**，"
    "该 skill 在其正文里**声明要的输入是否满足、前提是否具备**。\n"
    "重点：只判「输入要求 / 前提是否适配」，**不要**判该 skill 能否成功跑通、"
    "也不要判它好不好。\n"
    "输出要求（必须严格遵守）：\n"
    "1. 只输出一个 JSON 数组，不要任何解释、前后缀或代码块围栏；\n"
    "2. 数组每项形如 "
    '{"skill_id": "<候选 id>", "applicable": <true 或 false>, '
    '"confidence": <0到1之间的小数>, "reason": "<为何适用/不适用>"}；\n'
    "3. skill_id 必须是给定候选里真实存在的 id，不得编造；\n"
    "4. applicable 表示「输入要求是否满足」；confidence 是你对该判断的置信；\n"
    "5. 每个给定候选都要给出一项判断，适用与不适用都要列出。"
)


class SkillVerifyParseError(Exception):
    """LlmSkillVerifier 无法把模型回答解析为合法验证结果时抛出。

    触发场景：模型返回的文本不是合法 JSON、或顶层不是数组、或 provider 调用失败。
    **禁止**在这些情况下静默伪造默认结论——脏数据一律走显式错误，由调用方决定降级
    策略（遵循项目「禁 silent fallback」红线）。

    注意：单项级别的脏数据（候选外 id、缺 / 越界字段）按「丢弃该项」处理（见
    ``LlmSkillVerifier._parse_answer`` 注释），不抛本异常——整批可解析、个别项不合法
    属正常，不应让整次验证失败。
    """


class LlmSkillVerifier:
    """内核默认验证后端：起一次性子 LLM 调用，批量判候选的输入要求适配。

    实现 ``SkillVerifier`` 协议。把前 ``verify_max_candidates`` 个候选的
    ``skill_id + 完整 body``（超 ``verify_body_char_limit`` 截断）拼成验证 prompt，
    要求模型逐个判「就当前任务的输入 / 条件，该能力声明要的输入是否满足」并输出 JSON，
    再解析回 ``VerifiedCandidate`` 列表，**只保留 applicable=True** 的。

    定位（默认实现，可被业务替换）：
        - **非确定性**：底层是 LLM，相同 (task, candidates) 不保证相同结果。replay /
          测试需通过固定 model_client（如 SimClient）脚本化回放获得确定性；返回列表的
          **排序确定**（按 verify_confidence 降序、同分 skill_id 升序）。
        - **body 须能进一次 prompt**：本后端把精验候选的 body 全列进一次 prompt。护栏
          已限——只取前 ``verify_max_candidates`` 个、单 body 截到
          ``verify_body_char_limit``；万级超大场景应交外部服务（业务侧实现同协议）。

    body 取不到 / 截断的处理（禁伪造）：
        - ``get_body`` 返回 None（无 body）→ 该候选**直接判 applicable=False**（无 body
          无从验证输入要求），**不送 LLM**——按「仅返回 applicable=True」过滤后该候选自然
          不出现在结果里，既不浪费 token 也不伪造适配结论。
        - 单 body 超 ``verify_body_char_limit`` → 截断前缀进 prompt，并在该候选最终
          reason 追加 ``_BODY_TRUNCATED_NOTE`` 标记（审计可见）。

    解析策略（禁 silent fallback，仿 ``recall.py``）：
        - 整体非合法 JSON / 顶层非数组 / provider error → 抛 ``SkillVerifyParseError``。
        - 单项 ``skill_id`` ∉ 已验候选 → 丢弃。
        - 单项缺 ``applicable`` / ``confidence`` / ``reason``、类型非法、confidence 越界
          [0,1] → 丢弃该项（不伪造默认值）。
        - ``description`` / ``recall_confidence`` 取自入参 candidate（不信模型复述）。

    参照 codex/claw-code「内核只定协议、默认实现可被替换」范式；差异：本类用一次性 LLM
    调用做语义适配判定，依赖注入的 ``ModelClient`` 由业务侧提供（R1 业务零侵入）。
    """

    def __init__(
        self,
        model_client: ModelClient,
        *,
        model: str | None = None,
        verify_max_candidates: int = _DEFAULT_VERIFY_MAX_CANDIDATES,
        verify_body_char_limit: int = _DEFAULT_VERIFY_BODY_CHAR_LIMIT,
    ) -> None:
        """构造 LLM 验证后端。

        Args:
            model_client: 依赖注入的 ModelClient（业务侧构造，决定 provider / 鉴权）。
            model: 可选模型名覆盖；None 时由 client 用其构造时配置的默认模型。
            verify_max_candidates: 精验候选数上界（召回宽筛、验证精验少量，C2 护栏）。
            verify_body_char_limit: 单 body 入 prompt 的字符上限，超出截断。
        """
        self._client = model_client
        self._model = model
        self._verify_max_candidates = verify_max_candidates
        self._verify_body_char_limit = verify_body_char_limit

    def _collect_bodies(
        self,
        candidates: Sequence[SkillCandidate],
        get_body: Callable[[str], str | None],
    ) -> tuple[list[SkillCandidate], dict[str, str], dict[str, bool]]:
        """取前 N 个候选的 body：返回「有 body 的精验候选」+ body 文本 + 截断标记。

        无 body（get_body 返回 None）的候选直接排除出精验集（其结论由调用方按
        applicable=False 处理，本类不送 LLM、不伪造）。

        Args:
            candidates: 召回候选（已按相关度排序，取前 ``verify_max_candidates`` 个）。
            get_body: 按 skill_id 取完整 body 的回调。

        Returns:
            (verifiable, bodies, truncated)：
                verifiable —— 拿得到 body 的候选子集（保序）；
                bodies —— {skill_id: 截断后 body 文本}（供 prompt 构造复用）；
                truncated —— {skill_id: 该候选 body 是否被截断}。
        """
        head = list(candidates[: self._verify_max_candidates])
        verifiable: list[SkillCandidate] = []
        bodies: dict[str, str] = {}
        truncated: dict[str, bool] = {}
        for cand in head:
            body = get_body(cand.skill_id)
            if body is None:
                # 无 body：无从验证输入要求 → 不进精验集（调用方按不适用过滤掉）
                continue
            if len(body) > self._verify_body_char_limit:
                # 超长 body 截前缀；标记截断，结论 reason 注明
                body = body[: self._verify_body_char_limit]
                truncated[cand.skill_id] = True
            else:
                truncated[cand.skill_id] = False
            bodies[cand.skill_id] = body
            verifiable.append(cand)
        return verifiable, bodies, truncated

    def _build_request(
        self,
        task: str,
        verifiable: Sequence[SkillCandidate],
        bodies: dict[str, str],
    ) -> ApiRequest:
        """把 task + 精验候选（id + 截断后 body）拼成验证 prompt，构造一次性请求。

        Args:
            task: 当前任务 / 意图。
            verifiable: 拿得到 body 的精验候选（保序）。
            bodies: {skill_id: 截断后 body 文本}。

        Returns:
            单轮 user 消息的 ApiRequest（无工具、不并行工具调用）。
        """
        # 逐条列出候选 id 与（截断后）完整 body，编号便于模型对照（只回 id 不回编号）
        blocks = [
            f"{i + 1}. skill_id={c.skill_id}\nSKILL.md 正文：\n{bodies[c.skill_id]}"
            for i, c in enumerate(verifiable)
        ]
        candidates_block = "\n\n".join(blocks)
        content = (
            f"任务：{task}\n\n"
            f"候选 skill（共 {len(verifiable)} 个，逐个判断输入要求是否满足）：\n"
            f"{candidates_block}"
        )
        return ApiRequest(
            # 空字符串让 provider 用其构造时配置的默认 model（对齐 recall 路径）
            model=self._model or "",
            system_prompt=[_LLM_VERIFY_SYSTEM_PROMPT_ZH],
            messages=[ApiMessage(role="user", content=content)],
            tools=[],
            parallel_tool_calls=False,
        )

    async def _call_llm(self, request: ApiRequest, cancel: CancellationToken) -> str:
        """发起一次性 LLM 调用，累积并返回完整文本（参照 recall 的最简范式）。

        Args:
            request: 验证请求体。
            cancel: 级联取消 token（R4：长调用可中断，传给 session）。

        Returns:
            模型回答的完整文本（已 strip）。

        Raises:
            SkillVerifyParseError: provider 在流中 emit error 事件时（验证调用失败）。
            asyncio.CancelledError: cancel 期间由底层 session 抛出。
        """
        text = ""
        async with self._client.session(cancel=cancel, model=self._model) as session:
            async for ev in session.stream(request):
                if ev.kind == "text_delta":
                    text += str(ev.data.get("text", ""))
                elif ev.kind == "error":
                    # provider 侧失败：显式抛错，不静默返回空结论
                    raise SkillVerifyParseError(
                        f"LLM 验证调用失败：{ev.data.get('message')}"
                    )
        return text.strip()

    def _parse_answer(
        self,
        answer: str,
        verifiable: Sequence[SkillCandidate],
        truncated: dict[str, bool],
    ) -> list[VerifiedCandidate]:
        """把模型回答 JSON 解析为 VerifiedCandidate 列表（脏项丢弃、整体失败抛错）。

        Args:
            answer: 模型回答文本（期望 JSON 数组）。
            verifiable: 已送验的候选（用于校验 id ⊆ 池并取回可信 description / 长相分）。
            truncated: {skill_id: body 是否被截断}，截断的在 reason 注明。

        Returns:
            仅 applicable=True、按 verify_confidence 降序 / 同分 skill_id 升序 的列表。

        Raises:
            SkillVerifyParseError: answer 非合法 JSON、或顶层不是数组。
        """
        # 1) 整体解析：非合法 JSON → 显式失败（禁伪造默认结论）
        try:
            parsed: Any = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise SkillVerifyParseError(f"模型回答非合法 JSON：{exc}") from exc
        if not isinstance(parsed, list):
            raise SkillVerifyParseError(
                f"模型回答顶层应为 JSON 数组，实得 {type(parsed).__name__}"
            )

        # 2) 已验候选索引：id → candidate（取可信 description / recall_confidence）
        by_id = {c.skill_id: c for c in verifiable}

        # 3) 逐项校验：单项脏数据丢弃，不让整批失败
        results: list[VerifiedCandidate] = []
        for item in parsed:
            verified = self._parse_item(item, by_id, truncated)
            if verified is not None:
                results.append(verified)

        # 4) 确定性排序：verify_confidence 降序为主；同分 skill_id 升序（虽 LLM 非确定，
        #    但排序确定，便于断言 / replay）
        results.sort(key=lambda v: (-v.verify_confidence, v.skill_id))
        return results

    def _parse_item(
        self,
        item: Any,
        by_id: dict[str, SkillCandidate],
        truncated: dict[str, bool],
    ) -> VerifiedCandidate | None:
        """解析单条模型判断为 VerifiedCandidate；脏项 / 不适用返回 None。

        丢弃（返回 None）的情形：非对象、id 不在已验池、缺 / 非法 applicable /
        confidence / reason、confidence 越界、applicable=False（只保留适用）。

        Args:
            item: 模型回答数组里的一项。
            by_id: 已验候选 id → candidate 索引。
            truncated: {skill_id: body 是否被截断}。

        Returns:
            合法且 applicable=True 的 VerifiedCandidate；否则 None。
        """
        if not isinstance(item, dict):
            return None
        skill_id = item.get("skill_id")
        # id 必须是字符串且 ∈ 已验池（模型编造的池外 id 直接丢弃）
        if not isinstance(skill_id, str) or skill_id not in by_id:
            return None
        applicable = item.get("applicable")
        # applicable 必须是 bool（缺字段 / 非 bool 一律丢弃，不伪造）
        if not isinstance(applicable, bool):
            return None
        # 只保留适用的候选；不适用的直接滤掉（契约：仅返回 applicable=True）
        if not applicable:
            return None
        confidence = item.get("confidence")
        # confidence 必须是数值且 ∈ [0,1]（bool 是 int 子类，须显式排除）
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        conf_f = float(confidence)
        if not (_CONFIDENCE_MIN <= conf_f <= _CONFIDENCE_MAX):
            return None
        reason = item.get("reason")
        # reason 必须是非空字符串（缺 / 空 reason 丢弃——审计需要理由，不伪造占位）
        if not isinstance(reason, str) or not reason:
            return None
        # body 被截断 → reason 追加标记，让审计知道结论基于截断前缀
        if truncated.get(skill_id):
            reason = f"{reason}{_BODY_TRUNCATED_NOTE}"
        cand = by_id[skill_id]
        return VerifiedCandidate(
            skill_id=skill_id,
            # description / recall_confidence 取自入参 candidate（不信模型复述）
            description=cand.description,
            recall_confidence=cand.confidence,
            applicable=True,
            verify_confidence=conf_f,
            reason=reason,
        )

    async def verify(
        self,
        task: str,
        candidates: Sequence[SkillCandidate],
        *,
        get_body: Callable[[str], str | None],
        cancel: CancellationToken,
    ) -> list[VerifiedCandidate]:
        """据完整 body 起一次性 LLM 调用判输入要求适配（实现 SkillVerifier 协议）。

        流程：空候选短路 → 入口 check 取消 → 取前 N 候选 body（无 body 排除、超长截断）
        → 无可验候选短路 → 构造验证 prompt → 一次性 LLM 调用 → 解析 JSON → 仅
        applicable=True 的 VerifiedCandidate（脏项丢弃、整体失败抛错）。

        Args:
            task: 当前任务 / 意图（= search 的 query）。
            candidates: 召回候选（仅 description 级）。
            get_body: 按 skill_id 取完整 SKILL.md body 的回调；取不到返回 None。
            cancel: 级联取消 token；入口与 LLM 调用均传递，长调用可中断（R4）。

        Returns:
            仅 applicable=True、按 verify_confidence 降序 / 同分 skill_id 升序 的列表。

        Raises:
            SkillVerifyParseError: 模型回答非合法 JSON / 顶层非数组 / provider 调用失败。
            asyncio.CancelledError: cancel 已取消时抛出。
        """
        # 入口先让出调度点并检查取消，避免已取消仍发起验证
        await anyio.lowlevel.checkpoint()
        cancel.raise_if_cancelled()

        # 空候选短路：无候选可验，不浪费一次 LLM 调用
        if not candidates:
            return []

        # 取前 N 候选 body（无 body 排除、超长截断）
        verifiable, bodies, truncated = self._collect_bodies(candidates, get_body)
        # 无可验候选（全部拿不到 body）→ 短路，不发 LLM 调用
        if not verifiable:
            return []

        request = self._build_request(task, verifiable, bodies)
        answer = await self._call_llm(request, cancel)
        return self._parse_answer(answer, verifiable, truncated)
