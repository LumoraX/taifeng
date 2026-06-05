"""双轨 demo：同一批「核心步骤 skill」既走自治链、又走业务编排 retry。

⚠️ 需要真实 LLM key（读 ``.env`` 的 ``LLM_BOOTSTRAP_*``），**不进 CI**。

解决的矛盾（见 ../step_pipeline/README.md「三条路」之②）：
  taifeng 运行时 ``entry:true``（可被业务编排拉起）与「可被 ``call_skill`` 派发」
  （须 ``entry:false``）在同一 skill 上**互斥**。要两种模式共存 → **wrapper 双轨**：

    核心步骤 ``*_core``（entry:false）—— 真正的分析逻辑，单一真相
      ├─ 被 ``main``（entry:true）自治链 ``call_skill`` 依次派发  → 一键跑完
      └─ 被薄 wrapper ``intake/risk/plan``（entry:true）``call_skill`` → 业务编排 + 步级 retry

验证：
  轨道 A（自治链）：启动 ``main`` → 用 ``skill_dispatched`` 事件证明 3 个核心都被派发，
                    且 ``main`` 终报合并了三步结论（含各步 ⟦...⟧ 标记）。
  轨道 B（业务编排）：``Pipeline`` 拉起 3 个 wrapper → 顺跑 + retry 中间步 → 断言级联，
                    且 wrapper 原样回流了核心的 ⟦...⟧ 标记。

运行：PYTHONPATH=src uv run python examples/dual_track/demo.py
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
sys.path.insert(0, str(EXAMPLES))                       # _provider_bootstrap
sys.path.insert(0, str(EXAMPLES / "step_pipeline"))    # pipeline.Pipeline 复用

from _provider_bootstrap import build_model_client, load_dotenv_files  # noqa: E402
from pipeline import Pipeline  # noqa: E402

import taifeng  # noqa: E402
from taifeng.tool.builtins.request_user_input import (  # noqa: E402
    make_request_user_input_tool,
)

# 三步：(核心 id, wrapper id, 标题)
TRIPLES = [
    ("intake_core", "intake", "信息采集"),
    ("risk_core", "risk", "风险评估"),
    ("plan_core", "plan", "干预计划"),
]

# ── 核心步骤模板（entry:false，atomic）：真正的分析逻辑 + 唯一标记 ───────────
_CORE = """---
id: {cid}
name: {title}·核心
type: atomic
description: {title}的核心分析逻辑（供自治链与 wrapper 复用，单一真相）
---
你是肺结节功能医学分析的「{title}」核心单元。会收到【患者数据】，
以及可能存在的【前序结论】（上游步骤的输出）。

严格输出：
1. **第一行**：原样写出【患者数据】里的病例号（形如 CASE-XXXXXX）。
2. 然后用 3~4 句话完成「{title}」的专业分析（结合患者数据与前序结论）。
3. **最后一行**：输出本步骤标记 ⟦{tag}⟧

不要调用任何工具，直接输出文本结论。
"""

# ── 自治编排器（entry:true，composite）：一键依次 call_skill 三核心 ──────────
_MAIN = """---
id: main
name: 肺结节自治编排器
type: composite
entry: true
child_skills: [intake_core, risk_core, plan_core]
description: 一键自治链——依次派发 intake_core / risk_core / plan_core
---
你是肺结节分析的自治编排器。收到患者数据后，**必须依次**调用工具 call_skill：

1. call_skill(skill_id="intake_core", args=<患者数据原文>)
2. call_skill(skill_id="risk_core", args=<患者数据 + intake_core 的结论>)
3. call_skill(skill_id="plan_core", args=<患者数据 + 前两步结论>)

三步全部完成后，把三步结论合并成一份最终报告，
**务必原样保留每步结论里出现的所有 ⟦...⟧ 标记**（逐字复制，不要改写）。
"""

# ── wrapper（entry:true，composite）：业务编排入口，转调核心并原样回流 ────────
_WRAPPER = """---
id: {wid}
name: {title}·入口
type: composite
entry: true
child_skills: [{cid}]
description: 业务编排入口——转调 {cid} 并原样回流其结论（供步级 retry）
---
你是「{title}」步骤的业务编排入口。把你收到的**全部输入原文**作为 args，
调用工具 call_skill(skill_id="{cid}", args=<你收到的全部输入>)。

拿到 {cid} 返回的结论后，**原封不动**地把它作为你的最终回复输出
（包含其中的 ⟦...⟧ 标记与病例号），不要增删、不要改写、不要补充评论。
"""


def _bar(t: str) -> None:
    print("\n" + "═" * 70 + f"\n{t}\n" + "═" * 70)


def _write_skills(root: Path, tags: dict[str, str]) -> None:
    """生成 main + 3 核心 + 3 wrapper 共 7 个 SKILL.md。"""
    (root / "main").mkdir(parents=True)
    (root / "main" / "SKILL.md").write_text(_MAIN, encoding="utf-8")
    for cid, wid, title in TRIPLES:
        (root / cid).mkdir(parents=True)
        (root / cid / "SKILL.md").write_text(
            _CORE.format(cid=cid, title=title, tag=tags[cid]), encoding="utf-8")
        (root / wid).mkdir(parents=True)
        (root / wid / "SKILL.md").write_text(
            _WRAPPER.format(wid=wid, cid=cid, title=title), encoding="utf-8")


async def _run_autonomous(pool, seed: str, tags: dict[str, str]) -> None:
    """轨道 A：启动 main 自治链，用 skill_dispatched 事件证明 3 核心都派发。"""
    engine = await pool.get_or_create(session_id="dual:auto", entry_skill_id="main")
    sub = await engine.submit(taifeng.UserMessage(text=seed))
    dispatched: list[str] = []
    text_parts: list[str] = []
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub:
            continue
        k = ev.msg.kind
        d = ev.msg.data if hasattr(ev.msg, "data") else {}
        if k == "skill_dispatched":
            dispatched.append(d.get("skill_id", "?"))
            print(f"  → 自治链派发 call_skill({d.get('skill_id')}) "
                  f"depth={d.get('depth')} stack={d.get('stack_path')}")
        elif k == "assistant_text" and d.get("delta"):
            text_parts.append(str(d["delta"]))
        elif k == "turn_completed" and d.get("is_root"):
            break
        elif k == "turn_failed" and d.get("is_root"):
            raise RuntimeError(f"自治链失败: {d.get('error')}")
    final = "".join(text_parts).strip()
    print(f"\n  main 终报:\n  {final.replace(chr(10), chr(10) + '  ')}")

    core_ids = {cid for cid, _, _ in TRIPLES}
    print("\n  断言（轨道 A）:")
    assert core_ids.issubset(set(dispatched)), \
        f"自治链应派发全部 3 核心，实际={dispatched}"
    print(f"  ✓ 3 个核心都被自治链 call_skill 派发: {sorted(core_ids)}")
    for cid, _, title in TRIPLES:
        present = tags[cid] in final
        print(f"  {'✓' if present else '✗'} main 终报含 {title} 核心标记 "
              f"⟦{tags[cid]}⟧（核心输出回流到 main）")
        assert present, f"main 终报应含 {cid} 标记"


async def _run_business(pool, seed: str, tags: dict[str, str]) -> None:
    """轨道 B：Pipeline 拉起 3 wrapper，顺跑 + retry 中间步，断言级联 + 标记回流。"""
    pipe = Pipeline(
        pool,
        steps=[(wid, title) for _, wid, title in TRIPLES],
        seed=seed, base_session="dual:biz")
    await pipe.run_from(0)
    for s in pipe.steps:
        print(f"  顺跑 step{s.index}[{s.title}] {s.status} "
              f"thread={(s.thread_id or '')[:10]}… 输出={s.output_text[:30]!r}")
    assert all(s.status == "done" for s in pipe.steps), \
        [s.status for s in pipe.steps]

    print("\n  断言（轨道 B·顺跑）:")
    for i, (cid, _, title) in enumerate(TRIPLES):
        present = tags[cid] in pipe.steps[i].output_text
        print(f"  {'✓' if present else '✗'} wrapper {title} 原样回流了核心标记 "
              f"⟦{tags[cid]}⟧")
        assert present, f"wrapper {i} 应回流 {cid} 标记"

    print("\n  → retry 中间步（step 1 风险评估）")
    s1_thread_before = pipe.steps[0].thread_id
    await pipe.retry(1)
    for s in pipe.steps:
        print(f"  retry后 step{s.index}[{s.title}] {s.status} 尝试{s.attempt} "
              f"thread={(s.thread_id or '')[:10]}…")
    print("\n  断言（轨道 B·retry 级联）:")
    assert pipe.steps[0].thread_id == s1_thread_before
    print("  ✓ 上游 step0 未重跑（thread 不变）")
    assert pipe.steps[1].attempt == 2
    print("  ✓ 中间步 step1 重跑（尝试号→2）")
    assert tags["risk_core"] in pipe.steps[1].output_text
    print(f"  ✓ step1 重跑仍回流核心标记 ⟦{tags['risk_core']}⟧")
    assert pipe.steps[2].attempt == 2
    assert tags["plan_core"] in pipe.steps[2].output_text
    print("  ✓ 下游 step2 级联重跑、回流核心标记")


async def main() -> None:
    load_dotenv_files()
    client, meta = build_model_client(require_api_key=True)
    print(f"provider={meta['provider']} model={meta['model']}")

    case_id = f"CASE-{secrets.token_hex(3).upper()}"
    tags = {
        "intake_core": f"INTAKE:{secrets.token_hex(3).upper()}",
        "risk_core": f"RISK:{secrets.token_hex(3).upper()}",
        "plan_core": f"PLAN:{secrets.token_hex(3).upper()}",
    }
    print(f"本次随机 案例号={case_id}  核心标记={tags}")
    seed = (f"病例号 {case_id}。男 58 岁，体检发现右肺上叶磨玻璃结节 9mm，"
            "既往磺胺类药物过敏，长期夜班工作，无吸烟史。")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "skills"
        root.mkdir(parents=True)
        _write_skills(root, tags)

        pool = await taifeng.EnginePool.create(
            skills_dir=root, threads_dir=Path(td) / "t",
            model_client=client, compressors=[],
            extra_tools=[make_request_user_input_tool()], max_iterations=20)

        _bar("轨道 A：main 自治链——一键 call_skill 依次跑完 3 核心")
        await _run_autonomous(pool, seed, tags)

        _bar("轨道 B：业务编排——Pipeline 拉起 3 wrapper（转调同一批核心）+ 步级 retry")
        await _run_business(pool, seed, tags)

        await pool.close()

    _bar("结论")
    print("🎉 双轨验证通过：同一批核心步骤 skill")
    print("   • 轨道 A：被 main 自治链 call_skill 一键依次派发（3 核心全跑、结论回流）")
    print("   • 轨道 B：被 entry wrapper 拉起做业务编排，支持步级 retry + 级联")
    print("   → 自治模式与业务编排 retry 在【同一批核心】上共存（wrapper 双轨）")


if __name__ == "__main__":
    print("提示：本脚本调用真实 LLM（计费）。继续运行中…", file=sys.stderr)
    asyncio.run(main())
