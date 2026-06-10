"""聚焦自测：验证 entry:true 双重身份 + retry 中间步级联（纯 SimClient，无 API key）。

与同目录 demo.py 不同，本脚本专打两个容易出问题的点：

  1. **entry:true 双重身份**：同一个步骤 skill 既是编排 skill ``main`` 的 child
     （写进 ``main`` 的 child_skills），又自身 ``entry: true`` 可被业务单独拉起。
     验证：loader 不报错、validate 无环、两者都被识别为 entry、main 可达 child。

  2. **retry 中间步级联**：流水线 s1→s2→s3 跑完后，**只 retry 中间的 s2**，断言
     - s1（上游）**完全不动**：输出/尝试号/thread 都不变；
     - s2 重跑（尝试号 +1、新 thread、新输出 v2）；
     - s3（下游）级联重跑，且其【输入】含 s2 的【新】输出 v2（输入语义随上游更新）。

运行：PYTHONPATH=src uv run python examples/step_pipeline/verify_entry_retry.py
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import Pipeline  # noqa: E402  —— 同目录模块（脚本直跑）

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.skill.loader import load_skills_from_dir
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.types import ApiRequest

# ── 1. 在临时目录写 4 个 skill：1 个编排 main + 3 个步骤（双重身份）──────────
_ORCH = """---
id: main
name: 编排器
type: composite
entry: true
child_skills: [s1, s2, s3]
description: 自治链编排器（验证步骤 skill 同时作为 main 的 child）
---
你是编排器。需要时用 call_skill 依次派发 s1、s2、s3。
"""

# 步骤 skill 模板：既是 main 的 child，又 entry:true 的 tool-only composite
_STEP = """---
id: {sid}
name: {title}
type: composite
entry: true
tool_names: [request_user_input]
description: {title}（双重身份：main 的 child + 自身 entry）
---
你将收到【患者数据 + 上游结论】。完成「{title}」并给出结论。
"""


def _write_skills(root: Path) -> None:
    """在 root 下生成 main + s1/s2/s3 四个 SKILL.md。"""
    (root / "main").mkdir(parents=True)
    (root / "main" / "SKILL.md").write_text(_ORCH, encoding="utf-8")
    for sid, title in [("s1", "信息采集"), ("s2", "风险评估"), ("s3", "干预计划")]:
        (root / sid).mkdir(parents=True)
        (root / sid / "SKILL.md").write_text(
            _STEP.format(sid=sid, title=title), encoding="utf-8")


# ── 2. SimClient：RoutingSimClient 按 <entry_skill id="X"> 标记路由脚本，每标记一个游标 ───────
ROUTES: dict[str, list[SimTurn]] = {
    "s1": [SimTurn(text="### 采集结论：55岁/右肺结节8mm S1_OUT")],
    "s2": [
        SimTurn(text="### 风险评估 v1：基于[采集] → 中风险 S2_V1"),
        SimTurn(text="### 风险评估 v2（重试）：复核后 → 低风险 S2_V2"),
    ],
    "s3": [
        SimTurn(text="### 干预计划 v1 S3_V1"),
        SimTurn(text="### 干预计划 v2（级联重跑） S3_V2"),
    ],
}


# RoutingSimClient 按请求规范化全文的标记子串路由——skill body 进
# system_prompt,<entry_skill id="..."> 标记天然可作路由键,无需自定义 session


def _bar(t: str) -> None:
    print("\n" + "─" * 64 + f"\n{t}\n" + "─" * 64)


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "skills"
        _write_skills(root)

        # ── 验证点 1：entry skill 可作 session root（静态可加载）─────────
        # ⚠️ 注意：这里只证明「步骤 skill 标 entry:true 后能被业务编排单独拉起」。
        # 它**同时**写进 main.child_skills 仅在【静态加载】层面允许（不报错），
        # 但【运行时】main 无法真正 call_skill 它们 —— 见下方验证点 1b 的拒绝证据。
        _bar("验证 1/2：步骤 skill 标 entry:true → 可作 session root（业务编排单独拉起）")
        skills = load_skills_from_dir(root)
        ids = sorted(skills.keys())
        entries = sorted(s.id for s in skills.values() if s.entry)
        main = skills["main"]
        print(f"  加载 skills = {ids}")
        print(f"  entry skills = {entries}")
        print(f"  main.child_skills = {sorted(main.child_skills)}（静态关系，运行时不可达）")
        assert ids == ["main", "s1", "s2", "s3"], ids
        assert entries == ["main", "s1", "s2", "s3"], entries
        assert sorted(main.child_skills) == ["s1", "s2", "s3"], main.child_skills
        print("  ✓ s1/s2/s3 均 entry:true，可被业务编排（pool.get_or_create）单独拉起")

        # ── 验证点 1b：运行时 main.call_skill(entry 子) 必被拒（戳破"双重身份"）─
        from taifeng.skill.dispatch import CallStack, DispatchPolicy
        verdict = DispatchPolicy().check(
            CallStack().push("main", "call_1"), main, skills["s1"])
        print(f"  运行时 main.call_skill(s1[entry]) → "
              f"{'ALLOW' if verdict.allowed else 'REJECT/' + str(verdict.reason)}")
        # entry skill 不能被 call_skill 派发 → 自治链与业务编排在同一 skill 上互斥
        assert not verdict.allowed and verdict.reason == "cannot_call_entry_skill"
        print("  ✓ 运行时 call_skill→entry 被拒：自治链 vs 业务编排在同一 skill 上【互斥】")
        print("    （故 step_pipeline = 替换自治编排，非纯加法；详见 README「三条路」）")

        # ── 验证点 2：retry 中间步级联 ─────────────────────────────────
        pool = await taifeng.EnginePool.create(
            skills_dir=root, threads_dir=Path(td) / "t",
            model_client=RoutingSimClient(routes=ROUTES), compressors=[],
            extra_tools=[make_request_user_input_tool()], max_iterations=20)
        # 用 s1/s2/s3 作为业务编排的三步（每步 entry 单独跑）
        pipe = Pipeline(
            pool,
            steps=[("s1", "信息采集"), ("s2", "风险评估"), ("s3", "干预计划")],
            seed="男 55 岁，体检发现右肺结节 8mm",
            base_session="verify")

        _bar("验证 2/2：顺跑 s1→s2→s3，再【只 retry 中间步 s2】，断言上游不动 + 下游级联")
        await pipe.run_from(0)
        assert all(s.status == "done" for s in pipe.steps), [s.status for s in pipe.steps]
        for s in pipe.steps:
            print(f"  顺跑 step{s.index}[{s.title}] {s.status} 尝试{s.attempt} "
                  f"thread={s.thread_id[:10]}… 输出={s.output_text[:24]!r}")
        # 记录 retry 前快照
        s1_before = (pipe.steps[0].output_text, pipe.steps[0].attempt, pipe.steps[0].thread_id)
        s2_thread_v1 = pipe.steps[1].thread_id
        assert "S2_V1" in pipe.steps[1].output_text
        assert "S2_V1" in pipe.steps[2].input_text, "s3 顺跑输入应含 s2 v1 输出"

        print("\n  → 触发 pipe.retry(1)（只重试中间步 s2）")
        await pipe.retry(1)
        for s in pipe.steps:
            print(f"  retry后 step{s.index}[{s.title}] {s.status} 尝试{s.attempt} "
                  f"thread={s.thread_id[:10]}… 输出={s.output_text[:24]!r}")

        _bar("断言级联不变量")
        # A) 上游 s1 完全不动（输出/尝试号/thread 三者都不变）
        s1_after = (pipe.steps[0].output_text, pipe.steps[0].attempt, pipe.steps[0].thread_id)
        assert s1_after == s1_before, f"上游 s1 不应变化：{s1_before} → {s1_after}"
        print("  ✓ 上游 s1 完全不动（输出/尝试号/thread 均未变）")
        # B) s2 重跑：尝试号 +1、新 thread、新输出 v2
        assert pipe.steps[1].attempt == 2, pipe.steps[1].attempt
        assert pipe.steps[1].thread_id != s2_thread_v1, "s2 重试应换新 thread"
        assert "S2_V2" in pipe.steps[1].output_text, pipe.steps[1].output_text
        print("  ✓ 中间步 s2 重跑：尝试号→2、换新 thread、输出→v2")
        # C) 下游 s3 级联重跑，输入含 s2 的【新】输出 v2、不含旧 v1
        assert "S3_V2" in pipe.steps[2].output_text, pipe.steps[2].output_text
        assert "S2_V2" in pipe.steps[2].input_text, "s3 重试输入应含 s2 新输出 v2"
        assert "S2_V1" not in pipe.steps[2].input_text, "s3 重试输入不应再含旧 v1"
        print("  ✓ 下游 s3 级联重跑，输入语义随上游更新（含 S2_V2、不含 S2_V1）")
        # D) seed 始终保留在每步输入（session 语义保持）
        assert all("男 55 岁" in s.input_text for s in pipe.steps), "每步输入都应含 seed"
        print("  ✓ seed（患者数据）始终保留在每步输入中（session 语义保持）")

        await pool.close()
        print("\n🎉 entry:true 双重身份 + retry 中间步级联：全部断言通过")


if __name__ == "__main__":
    asyncio.run(main())
