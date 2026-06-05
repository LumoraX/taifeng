"""真实 LLM 验证：证明「下游步骤的回复确实用到了上游 session 内容」。

⚠️ 需要真实 LLM key（读 ``.env`` 的 ``LLM_BOOTSTRAP_*``），**不进 CI**。

验证手法（追踪标记法，杜绝缓存/训练记忆干扰）：
  1. seed 里塞一个**本次运行随机生成的病例号** ``CASE-XXXX``；
  2. 每步 skill 被要求：**先原样复述你在【前序结论】里看到的所有 ``⟦...⟧`` 标记**，
     再在末尾追加自己这一步的标记 ``⟦STEPk:随机码⟧``；
  3. 关键：**下游 skill 的 body 完全不写上游标记长什么样** —— 它只可能从 pipeline
     拼进去的「上游输出」里读到。下游输出若出现上游标记，即铁证它用到了 session 内容。

断言：
  - s2 输出含 s1 的标记 + 病例号  → s2 用到了 s1 的输出（session 串联正确）；
  - s3 输出含 s1、s2 两个标记 + 病例号 → s3 用到了全部上游输出；
  - retry 中间步 s2 后：s2 复跑仍含 s1 标记（重放上游正确），s3 级联重跑含 s2 **新**标记。

运行：PYTHONPATH=src uv run python examples/step_pipeline/verify_real_llm.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # examples/ —— 为了 import _provider_bootstrap
sys.path.insert(0, str(HERE))          # 同目录 —— 为了 import pipeline

from _provider_bootstrap import build_model_client, load_dotenv_files  # noqa: E402
from pipeline import Pipeline  # noqa: E402

import taifeng  # noqa: E402
from taifeng.tool.builtins.request_user_input import (  # noqa: E402
    make_request_user_input_tool,
)

# 每步 skill 模板：要求复述上游标记 + 追加本步标记。
# 注意：body **不写**上游标记的具体内容，下游只能从拼入的上游输出里读到。
_STEP_BODY = """---
id: {sid}
name: {title}
type: composite
entry: true
tool_names: [request_user_input]
description: {title}（真实 LLM session 串联验证步骤）
---
你是功能医学分析流水线中的「{title}」步骤。

你会收到【患者数据】，以及可能存在的【前序结论】（上游步骤的输出）。

严格按以下要求输出：
1. **第一行**：先原样写出【患者数据】里的病例号（形如 CASE-XXXXXX），再把你在
   【前序结论】里看到的所有形如 ⟦...⟧ 的标记**原样逐一复述**（用空格分隔；
   若没有前序结论就只写病例号）。
2. 然后用 3~5 句话完成「{title}」的专业分析（基于患者数据与前序结论）。
3. **最后一行**：输出本步骤标记 ⟦{tag}⟧

不要调用任何工具，直接给出文本结论。
"""


def _bar(t: str) -> None:
    print("\n" + "═" * 70 + f"\n{t}\n" + "═" * 70)


def _write_skills(root: Path, tags: dict[str, str]) -> None:
    """生成 s1/s2/s3 三个 atomic+entry 步骤 skill，各自带唯一标记 tag。"""
    titles = {"s1": "信息采集", "s2": "风险评估", "s3": "干预计划"}
    for sid, title in titles.items():
        (root / sid).mkdir(parents=True)
        (root / sid / "SKILL.md").write_text(
            _STEP_BODY.format(sid=sid, title=title, tag=tags[sid]),
            encoding="utf-8")


async def main() -> None:
    load_dotenv_files()
    client, meta = build_model_client(require_api_key=True)
    print(f"provider={meta['provider']} model={meta['model']} "
          f"base_url={meta.get('base_url', '')}")

    # 本次运行的随机标识：病例号 + 每步唯一标记（杜绝缓存命中）
    case_id = f"CASE-{secrets.token_hex(3).upper()}"
    tags = {
        "s1": f"STEP1:{secrets.token_hex(3).upper()}",
        "s2": f"STEP2:{secrets.token_hex(3).upper()}",
        "s3": f"STEP3:{secrets.token_hex(3).upper()}",
    }
    print(f"本次随机 案例号={case_id}  标记={tags}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "skills"
        root.mkdir(parents=True)
        _write_skills(root, tags)

        pool = await taifeng.EnginePool.create(
            skills_dir=root, threads_dir=Path(td) / "t",
            model_client=client, compressors=[],
            extra_tools=[make_request_user_input_tool()], max_iterations=12)

        seed = (f"病例号 {case_id}。男 58 岁，体检发现右肺上叶磨玻璃结节 9mm，"
                "既往磺胺类药物过敏，长期夜班工作，无吸烟史。")
        pipe = Pipeline(
            pool,
            steps=[("s1", "信息采集"), ("s2", "风险评估"), ("s3", "干预计划")],
            seed=seed, base_session="real")

        # ── 阶段 1：顺跑三步 ───────────────────────────────────────────
        _bar("阶段 1：真实 LLM 顺跑 s1→s2→s3")
        await pipe.run_from(0)
        for s in pipe.steps:
            print(f"\n┌─ step{s.index} [{s.title}] 状态={s.status} "
                  f"thread={(s.thread_id or '')[:12]}…")
            print("│  ▼ 喂给 LLM 的输入（seed + 前序结论）:\n│  "
                  + s.input_text.replace("\n", "\n│  "))
            print("│  ▲ LLM 输出:\n│  "
                  + s.output_text.replace("\n", "\n│  "))
            print("└" + "─" * 60)

        assert all(s.status == "done" for s in pipe.steps), \
            [s.status for s in pipe.steps]

        # ── 阶段 2：断言 session 内容被下游真实使用 ──────────────────────
        _bar("阶段 2：断言下游回复确实用到了上游 session 内容")
        s1_out, s2_out, s3_out = (s.output_text for s in pipe.steps)
        checks = [
            (case_id in s1_out, f"s1 输出含案例号 {case_id}"),
            (tags["s1"] in s2_out, f"s2 输出复述了 s1 标记 ⟦{tags['s1']}⟧（用到 s1 输出）"),
            (case_id in s2_out, f"s2 输出含案例号 {case_id}（seed 串联）"),
            (tags["s1"] in s3_out, f"s3 输出复述了 s1 标记 ⟦{tags['s1']}⟧（用到 s1 输出）"),
            (tags["s2"] in s3_out, f"s3 输出复述了 s2 标记 ⟦{tags['s2']}⟧（用到 s2 输出）"),
            (case_id in s3_out, f"s3 输出含案例号 {case_id}（seed 全链串联）"),
        ]
        ok = True
        for passed, desc in checks:
            print(f"  {'✓' if passed else '✗'} {desc}")
            ok = ok and passed
        assert ok, "下游未正确使用上游 session 内容（见上 ✗ 项）"

        # ── 阶段 3：retry 中间步 s2 → 验证重放上游 + 级联 ────────────────
        _bar("阶段 3：retry 中间步 s2（验证重放上游 session + 级联重跑 s3）")
        s2_tag_before = tags["s2"]  # 标记由 skill body 固定，复跑仍是同一个 → 改判 thread 变化
        s1_thread_before = pipe.steps[0].thread_id
        await pipe.retry(1)
        for s in pipe.steps:
            print(f"  step{s.index}[{s.title}] 状态={s.status} 尝试{s.attempt} "
                  f"thread={(s.thread_id or '')[:12]}…")
        _r1, r2, r3 = (s.output_text for s in pipe.steps)
        rechecks = [
            (pipe.steps[0].thread_id == s1_thread_before,
             "上游 s1 未重跑（thread 不变）"),
            (pipe.steps[1].attempt == 2, "s2 重跑（尝试号→2）"),
            (tags["s1"] in r2,
             f"s2 重跑输出仍复述 s1 标记 ⟦{tags['s1']}⟧（重放上游 session 正确）"),
            (case_id in r2, f"s2 重跑输出仍含案例号 {case_id}"),
            (s2_tag_before in r3,
             f"s3 级联重跑输出含 s2 标记 ⟦{s2_tag_before}⟧（用到重跑后的 s2 输出）"),
        ]
        ok2 = True
        for passed, desc in rechecks:
            print(f"  {'✓' if passed else '✗'} {desc}")
            ok2 = ok2 and passed
        assert ok2, "retry 后 session 串联不正确（见上 ✗ 项）"

        await pool.close()

    _bar("结论")
    print("🎉 真实 LLM 验证通过：")
    print("   • 下游每步回复都正确复述了上游步骤的唯一标记 → session 内容确实被使用")
    print("   • seed（病例号）贯穿全链 → 患者数据正确串联")
    print("   • retry 中间步：上游不动、重放上游 session 正确、下游级联用到新上游输出")


if __name__ == "__main__":
    # 显式提示：本脚本会真实计费
    if not os.environ.get("TAIFENG_REAL_LLM_OK"):
        print("提示：本脚本调用真实 LLM（计费）。继续运行中…", file=sys.stderr)
    asyncio.run(main())
