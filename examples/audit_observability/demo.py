"""审计可观测 层1 端到端示例 —— 业务端「直接订阅完整审计流」。

演示一个业务侧 ``CompleteAuditSink`` 如何拿到一次会话的**全部**事件用于审计：

  1. **完整订阅**：订阅 firehose ``engine.subscribe_all_envelopes()`` —— 收该 engine
     （= 单 session）的全部事件，覆盖 turn 生命周期 / tool 调用 / skill 派发 / HITL /
     压缩 / 以及 LLM **request 全文**（开 ``enable_request_capture`` 后）。
  2. **全局唯一落库键**：每条审计记录用 ``(session_id, seq)``。``session_id`` 由订阅方
     在 attach 时按所属 engine 捕获（``engine.session_id``），**不**盖在事件上。
  3. **丢失自检**：用 per-subscriber 的 ``delivery_seq`` 连续性自检——队列满丢弃会「烧号」，
     收到的 delivery_seq 一旦跳号即 = 本订阅自己漏了事件（与全局 seq 跳号=过滤 互不混淆）。
  4. **request 留痕**：``enable_request_capture=True`` 后，每次实际发往 provider 的
     request（``ApiRequest`` 全文）在发送前 emit 一条 ``llm_request_recorded``。

可靠落盘 / 脱敏 / 加密 / 保留期 / 按 seq 补丢失 全归业务消费者（内核只留痕、不治理）。
本例把审计记录收进内存列表并打印 + 断言，生产可换 Redis Stream / Kafka / DB。

运行（端到端，无需 LLM API key）：

    PYTHONPATH=src uv run python examples/audit_observability/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.types import TokenUsage

# ====================================================================
# 业务侧「完整审计订阅」实现
# ====================================================================


class CompleteAuditSink:
    """订阅 firehose，把整条会话的事件流落成可对账的审计记录。

    必须订阅 ``subscribe_all_envelopes``（全量 firehose）——这是「完整审计」的硬前提：
    只有全量流的全局 ``seq`` 才连续可自检；过滤订阅只收子集，seq 天然跳号（=过滤非丢弃）。
    """

    def __init__(self, session_id: str) -> None:
        # session_id 在 attach 时按所属 engine 捕获（事件本身不带，见契约 §4.3）
        self._session_id = session_id
        self._expect_delivery = 0          # 期望的下一个 delivery_seq（连续性基线）
        self.records: list[dict[str, object]] = []
        self.gaps: list[tuple[int, int]] = []   # 自检到的丢失区间（本订阅漏了哪些）

    async def run(self, engine: taifeng.AgentEngine) -> None:
        """drain firehose 直到 shutdown。每条事件落一条审计记录 + 丢失自检。"""
        async for env in engine.subscribe_all_envelopes():
            ev = env.event
            # ① 丢失自检：delivery_seq 跳号 = 本订阅自己漏了事件
            if env.delivery_seq != self._expect_delivery:
                self.gaps.append((self._expect_delivery, env.delivery_seq))
            self._expect_delivery = env.delivery_seq + 1

            # ② 落审计记录：全局唯一键 (session_id, seq) + 事件类型
            record: dict[str, object] = {
                "audit_key": (self._session_id, ev.seq),  # 全局唯一，可作 DB 主键
                "kind": ev.msg.kind,
                "submission_id": ev.submission_id,
            }
            # ③ request 全文留痕：含完整 prompt + conversation（敏感，业务自负脱敏/加密）
            if ev.msg.kind == "llm_request_recorded":
                data = ev.msg.data
                record["llm_request"] = {
                    "model": data.get("model"),
                    "messages": data.get("messages"),
                }
            self.records.append(record)

            if ev.msg.kind == "shutdown":
                return


# ====================================================================
# 最小可跑 skill 目录（无需真实 LLM）
# ====================================================================

ENTRY_SKILL = """---
name: general
description: 通用入口 skill
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [helper]
tool_names: []
max_call_depth: 2
---
# General
You are a helpful assistant.
"""

CHILD_SKILL = """---
name: helper
description: 辅助 atomic skill
version: 1.0.0
type: atomic
---
# Helper
辅助内容。
"""


def _prepare_skills(skills_dir: Path) -> None:
    (skills_dir / "general").mkdir(parents=True, exist_ok=True)
    (skills_dir / "general" / "SKILL.md").write_text(ENTRY_SKILL, encoding="utf-8")
    (skills_dir / "helper").mkdir(parents=True, exist_ok=True)
    (skills_dir / "helper" / "SKILL.md").write_text(CHILD_SKILL, encoding="utf-8")


# ====================================================================
# main
# ====================================================================


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skills_dir = root / "skills"
        _prepare_skills(skills_dir)

        client = SimClient(turns=[
            SimTurn(text=f"结论 {i}", usage=TokenUsage(input_tokens=12, output_tokens=4))
            for i in range(2)
        ])

        # 关键：enable_request_capture=True 才会 emit request 全文留痕（默认关=零泄漏面）
        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=root / "threads",
            model_client=client,
            compressors=[],
            enable_request_capture=True,
        )
        engine = await pool.get_or_create(
            session_id="audit-demo", entry_skill_id="general",
        )

        # attach 审计 sink：session_id 在边界从 engine 取（事件不带）
        audit = CompleteAuditSink(engine.session_id)
        audit_task = asyncio.create_task(audit.run(engine))
        await asyncio.sleep(0)  # 让 firehose 队列注册，避免漏首批事件

        # 跑 2 个 turn
        for i in range(2):
            sub_id = await engine.submit(taifeng.UserMessage(text=f"问题 {i}"))
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break

        await pool.close()              # 触发 shutdown → 审计任务收尾
        await asyncio.wait_for(audit_task, timeout=5.0)

        # ---- 结果展示 + 断言 ----
        kinds = [r["kind"] for r in audit.records]
        reqs = [r for r in audit.records if r["kind"] == "llm_request_recorded"]
        seqs = [r["audit_key"][1] for r in audit.records]  # type: ignore[index]

        print(f"✓ 完整审计流共 {len(audit.records)} 条事件（覆盖 {len(set(kinds))} 种 kind）")
        print(f"  事件 kind: {sorted(set(kinds))}")
        print(f"  request 全文留痕: {len(reqs)} 条（每次实发各一条）")
        print(f"  落库键样例 (session_id, seq): {[r['audit_key'] for r in audit.records[:3]]}")
        print(f"  全局 seq 连续: {seqs == list(range(seqs[0], seqs[0] + len(seqs)))}")
        print(f"  本订阅丢失自检 gaps: {audit.gaps or '无（delivery_seq 全程连续，零丢失）'}")
        if reqs:
            first_req = reqs[0]["llm_request"]
            print(f"  首条 request: model={first_req['model']!r} "  # type: ignore[index]
                  f"messages 条数={len(first_req['messages'])}")  # type: ignore[index]

        # 断言：审计完整、无丢失、request 全文在
        assert not audit.gaps, "delivery_seq 连续性自检应为零丢失"
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "全局 seq 应连续"
        assert len(reqs) >= 2, "2 个 turn 至少 2 条 request 留痕"
        assert reqs[0]["llm_request"]["messages"], "request 应含全文 messages"  # type: ignore[index]
        assert "turn_completed" in kinds and "llm_request_recorded" in kinds
        print("\n🎉 完整审计订阅演示完毕："
              "(session_id, seq) 落库 + delivery_seq 自检 + request 全文留痕")


if __name__ == "__main__":
    asyncio.run(main())
