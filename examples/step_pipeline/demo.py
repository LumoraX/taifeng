"""离线演示 + 自测：业务编排多步流水线 + 步级级联重试（纯 SimClient，无需 API key）。

讲一个故事：
  1. 跑 intake→risk→plan 三步；intake 第 1 步调 request_user_input 弹表单 → 挂起。
  2. 用户填表 → resume → intake 完成 → 自动顺跑 risk、plan。
  3. **重试 step 0（intake）**：作废 0..2，用各自重新构造的输入级联重跑 intake/risk/plan。
  4. 断言关键不变量：重试后 risk 的【输入】包含 intake 的【新】输出（cascade 输入语义正确），
     且每步 thread 随尝试号变化（独立重跑、不污染旧 thread）。

运行：PYTHONPATH=src uv run python examples/step_pipeline/demo.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import Pipeline  # noqa: E402  —— 同目录模块（脚本直跑）

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

if TYPE_CHECKING:

    pass

SKILLS = Path(__file__).resolve().parent / "skills"

# intake 的表单 schema（动态题型示例）
_FORM = {
    "type": "object",
    "properties": {
        "age": {"type": "string", "title": "年龄"},
        "smoking": {"type": "string", "title": "吸烟史", "enum": ["从不", "已戒", "目前吸烟"]},
    },
    "required": ["age"],
}

# 按 entry skill id 路由的脚本（每 skill 一个游标）
ROUTES: dict[str, list[SimTurn]] = {
    '<entry_skill id="intake"': [
        SimTurn(text="信息不全，先采集", tool_calls=[{
            "id": "rui_intake", "name": "request_user_input",
            "arguments": '{"prompt":"请补充基础信息","response_schema":'
                         + __import__("json").dumps(_FORM, ensure_ascii=False) + "}"}]),
        SimTurn(text="### 采集结果 v1：55岁/目前吸烟/右肺结节8mm INTAKE_V1"),
        SimTurn(text="### 采集结果 v2（重试）：55岁/已戒烟/右肺结节8mm INTAKE_V2"),
    ],
    '<entry_skill id="risk"': [
        SimTurn(text="### 风险评估 v1：基于[采集v1] → 中风险 RISK_V1"),
        SimTurn(text="### 风险评估 v2：基于[采集v2] → 低风险 RISK_V2"),
    ],
    '<entry_skill id="plan"': [
        SimTurn(text="### 干预计划 v1 PLAN_V1"),
        SimTurn(text="### 干预计划 v2 PLAN_V2"),
    ],
}


# RoutingSimClient 按请求规范化全文的标记子串路由——skill body 进
# system_prompt,<entry_skill id="..."> 标记天然可作路由键,无需自定义 session


def _bar(t: str) -> None:
    print("\n" + "─" * 60 + f"\n{t}\n" + "─" * 60)


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS, threads_dir=Path(td) / "t",
            model_client=RoutingSimClient(routes=ROUTES), compressors=[],
            extra_tools=[make_request_user_input_tool()], max_iterations=20)
        pipe = Pipeline(
            pool,
            steps=[("intake", "信息采集"), ("risk", "风险评估"), ("plan", "干预计划")],
            seed="男 55 岁，体检发现右肺结节 8mm",
            base_session="demo")

        _bar("阶段 1/4：顺跑 —— intake 第 1 步弹表单 → 挂起")
        await pipe.run_from(0)
        s0 = pipe.steps[0]
        print(f"step0[{s0.title}] 状态={s0.status}  thread={s0.thread_id[:12]}…")
        assert s0.status == "suspended", s0.status
        print(f"  弹出表单 prompt={s0.pending['prompt']!r}")
        props = s0.pending["response_schema"]["properties"]
        kinds = [(k, "单选" if "enum" in v else "问答") for k, v in props.items()]
        print(f"  表单题型: {kinds}")

        _bar("阶段 2/4：用户填表 → resume → 自动顺跑 risk、plan")
        await pipe.resume_step(0, s0.pending["request_id"], {"age": "55", "smoking": "目前吸烟"})
        for s in pipe.steps:
            print(f"step{s.index}[{s.title}] 状态={s.status}  输出={s.output_text[:40]!r}")
        assert all(s.status == "done" for s in pipe.steps), [s.status for s in pipe.steps]
        v1_threads = [s.thread_id for s in pipe.steps]
        # 验证 risk 的输入确实包含 intake v1 输出（输入语义正确）
        assert "INTAKE_V1" in pipe.steps[1].input_text, "risk 输入应含 intake v1 输出"
        print(f"\n  ✓ risk 输入含 intake v1 输出: {'INTAKE_V1' in pipe.steps[1].input_text}")

        _bar("阶段 3/4：重试 step 0（intake）→ 级联重跑 intake/risk/plan")
        await pipe.retry(0)
        for s in pipe.steps:
            print(f"step{s.index}[{s.title}] {s.status} 尝试{s.attempt} {s.output_text[:30]!r}")

        _bar("阶段 4/4：断言级联重试的不变量")
        # 1) 三步都重跑完成
        assert all(s.status == "done" for s in pipe.steps), [s.status for s in pipe.steps]
        # 2) intake 出了 v2、risk 出了 v2（级联重跑）
        assert "INTAKE_V2" in pipe.steps[0].output_text
        assert "RISK_V2" in pipe.steps[1].output_text
        # 3) 关键：重试后 risk 的【输入】包含 intake 的【新】输出 v2（cascade 输入语义随上游更新）
        assert "INTAKE_V2" in pipe.steps[1].input_text, "risk 重试输入应含 intake v2 输出"
        assert "INTAKE_V1" not in pipe.steps[1].input_text, "risk 重试输入不应再含旧 v1"
        # 4) 每步用了新 thread（独立重跑，不污染旧 thread）
        v2_threads = [s.thread_id for s in pipe.steps]
        assert all(a != b for a, b in zip(v1_threads, v2_threads, strict=True)), "重试应换新 thread"
        print("✓ 三步级联重跑完成")
        print("✓ risk 重试输入含 intake 新输出 INTAKE_V2、不含旧 INTAKE_V1（输入语义随上游更新）")
        print("✓ 每步换了新 thread（重跑隔离）")
        await pool.close()
        print("\n🎉 业务编排 + 步级级联重试：全部断言通过")


if __name__ == "__main__":
    asyncio.run(main())
