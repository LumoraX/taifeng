"""业务编排式多步流水线 —— 每步独立可调试重试（cascade）。

与「entry skill 自治 call_skill 跑完整链」相对：这里把**编排（何时跑下一步）下沉到
业务层**，每个步骤 skill 作为**独立 entry** 单独跑一个 turn / thread。好处：

- **输入语义可控**：每步输入 = ``seed（患者数据）+ 前序步骤输出``，由业务**显式构造并
  持久化**（见 ``Step.input_text``），重试时**原样重放**该输入 —— 不是参数盲覆盖。
- **步级重试（cascade）**：``retry(k)`` 作废 ``k..N`` 的旧结果，用各自重新构造的输入从
  ``k`` 往后重跑（下游依赖上游，上游变了下游必须重算）。
- **HITL 融入**：某步内 ``request_user_input`` 会挂起**该步自己的 root thread**（步骤是
  entry，无嵌套），业务侧 ``resume_step`` 提交 ``Resume(thread, {rid: payload})`` 续跑。

本模块**不绑定 web**：``run_from`` / ``retry`` / ``resume_step`` 接一个 ``forward``
回调把内核 EventMsg 透出去（web 推 SSE / 离线 demo 打印）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.loop.submission import Resume

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine

# forward 回调：把 (step_index, event_dict) 透给上层（web SSE / 打印）。None=不透出。
ForwardFn = Callable[[int, dict[str, Any]], Awaitable[None]] | None


@dataclass
class Step:
    """单个步骤的运行态（含持久化的输入/输出，供重试重放）。"""

    index: int
    skill_id: str
    title: str
    input_text: str = ""          # 持久化输入 = seed + 前序输出（重试时原样重放）
    output_text: str = ""
    status: str = "pending"       # pending | running | suspended | done | error
    thread_id: str | None = None
    attempt: int = 0
    pending: dict[str, Any] | None = None  # 挂起时 {request_id, prompt, response_schema}

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "skill_id": self.skill_id, "title": self.title,
            "input_text": self.input_text, "output_text": self.output_text,
            "status": self.status, "thread_id": self.thread_id,
            "attempt": self.attempt, "pending": self.pending,
        }


class Pipeline:
    """一条多步流水线的编排器（单 seed 驱动；步骤顺序固定）。"""

    def __init__(
        self,
        pool: taifeng.EnginePool,
        steps: list[tuple[str, str]],   # [(skill_id, title), ...]
        seed: str,
        base_session: str,
    ) -> None:
        self.pool = pool
        self.seed = seed
        self.base = base_session
        self.steps = [Step(i, sid, title) for i, (sid, title) in enumerate(steps)]

    # ── 输入构造（核心：显式 seed + 前序输出，可重放）──────────────────
    def _input_for(self, index: int) -> str:
        """构造第 ``index`` 步的输入文本 = 患者数据 + 已完成前序步骤的结论。"""
        parts = [f"【患者数据】\n{self.seed}"]
        prior = [
            f"【步骤{j + 1}·{self.steps[j].title} 结论】\n{self.steps[j].output_text}"
            for j in range(index) if self.steps[j].status == "done"
        ]
        if prior:
            parts.append("【前序结论】\n" + "\n\n".join(prior))
        return "\n\n".join(parts)

    # ── 顺跑：从 index 跑到末尾或中途挂起/失败 ─────────────────────────
    async def run_from(self, index: int, forward: ForwardFn = None) -> None:
        """从第 ``index`` 步顺序往后跑；某步挂起/失败则停（等 resume/retry）。"""
        for k in range(index, len(self.steps)):
            self.steps[k].input_text = self._input_for(k)
            status = await self._run_step(k, forward)
            if status != "done":
                return  # suspended / error → 停在这里

    # ── 重试：作废 k..N，从 k 往后级联重跑 ────────────────────────────
    async def retry(self, index: int, forward: ForwardFn = None) -> None:
        """重试第 ``index`` 步：作废 ``index..N`` 旧结果，从 ``index`` 往后重跑。

        下游每步的输入会用「重跑后的上游输出」重新构造（cascade），保证语义一致。
        """
        for k in range(index, len(self.steps)):
            self.steps[k].status = "pending"
            self.steps[k].output_text = ""
            self.steps[k].pending = None
        await self.run_from(index, forward)

    # ── 续跑：某步表单挂起 → 用户填写后 Resume，再往后顺跑 ──────────────
    async def resume_step(
        self, index: int, request_id: str, payload: dict[str, Any],
        forward: ForwardFn = None,
    ) -> None:
        """提交 ``Resume`` 续跑挂起的第 ``index`` 步；完成后自动往后顺跑。"""
        st = self.steps[index]
        if st.status != "suspended" or st.thread_id is None:
            raise ValueError(f"step {index} 非挂起态，不能 resume")
        engine = await self._engine_for(index, st.attempt, st.skill_id)
        sub = await engine.submit(Resume(
            thread_id=st.thread_id, resolutions={request_id: payload}))
        status = await self._drive(index, engine, sub, forward)
        if status == "done":
            await self.run_from(index + 1, forward)

    # ── 运行单步：独立 engine/thread，驱动到终结 ──────────────────────
    async def _run_step(self, index: int, forward: ForwardFn) -> str:
        st = self.steps[index]
        st.attempt += 1
        st.status = "running"
        st.output_text = ""
        st.pending = None
        engine = await self._engine_for(index, st.attempt, st.skill_id)
        st.thread_id = engine.thread_id
        sub = await engine.submit(taifeng.UserMessage(text=st.input_text))
        return await self._drive(index, engine, sub, forward)

    async def _engine_for(self, index: int, attempt: int, skill_id: str) -> AgentEngine:
        """为「步 index 的第 attempt 次尝试」取/建独立 engine（独立 thread）。"""
        return await self.pool.get_or_create(
            session_id=f"{self.base}:s{index}:a{attempt}", entry_skill_id=skill_id)

    async def _drive(
        self, index: int, engine: AgentEngine, sub_id: str, forward: ForwardFn,
    ) -> str:
        """订阅该 submission 直到终结：累积 assistant 文本，识别 完成/挂起/失败。

        返回 "done" | "suspended" | "error"，并把状态写回 ``self.steps[index]``。
        """
        st = self.steps[index]
        text_parts: list[str] = []
        async for ev in engine.subscribe_all():
            if ev.submission_id != sub_id:
                continue
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if forward is not None:
                await forward(index, {"kind": ev.msg.kind, "data": data})
            if ev.msg.kind == "assistant_text" and data.get("delta"):
                text_parts.append(str(data["delta"]))
            # 挂起（该步是 entry，request_user_input 挂的是其 root thread）
            if ev.msg.kind == "turn_suspended" and data.get("thread_id") == engine.thread_id:
                pend = next((p for p in data.get("pending", [])
                             if p.get("reason") in ("data", "form")), None)
                if pend is not None:
                    st.status = "suspended"
                    st.pending = {
                        "request_id": pend["request_id"],
                        "prompt": pend.get("detail", {}).get("prompt", ""),
                        "response_schema": pend.get("detail", {}).get("response_schema", {}),
                    }
                    return "suspended"
            if ev.msg.kind == "turn_completed" and data.get("is_root"):
                st.output_text = "".join(text_parts).strip()
                st.status = "done"
                return "done"
            if ev.msg.kind == "turn_failed" and data.get("is_root"):
                st.output_text = f"[失败] {data.get('error', '')}"
                st.status = "error"
                return "error"
        return "error"

    def snapshot(self) -> list[dict[str, Any]]:
        """当前所有步骤状态（供前端渲染步卡）。"""
        return [s.to_dict() for s in self.steps]


async def _noop_forward(index: int, ev: dict[str, Any]) -> None:  # pragma: no cover
    await asyncio.sleep(0)
