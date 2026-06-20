"""验证门拒绝**理由**探针 —— dump 逐候选原始裁决（含 applicable=False 的 reason）。

为什么要这个脚本：``LlmSkillVerifier.verify`` 按契约**只返回 applicable=True**，把被拒
候选连同 reason 一起滤掉了——所以 ``bench_verify_value.py`` 只看得到「SAT 被拦成 None」，
看不到「**为什么**拦」。本探针绕开过滤，直接调验证器内部（``_collect_bodies`` →
``_build_request`` → ``_call_llm``）拿**原始 JSON**，把每个候选的 applicable + reason 全打出来。

目的：判定「SAT 被拦」的根因是哪一种——
- (A) **拦得对**：reason 实质是「任务只是文字声称有附件、并无真实图像/音频字节」，
  这在纯文本 API 调用下**事实成立**，拦截合理，锅在合成数据前提（SAT 假设「声称=有输入」）。
- (B) **拦得过苛**：reason 是误读输入要求 / 强加未声明的前提 / 把「描述够具体」也判否，
  那是验证器提示词过严、可校准。

运行（需真实 LLM key）：
    PYTHONPATH=src python examples/real_llm/skill_select/probe_verify_reasons.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

from taifeng.loop.cancellation import CancellationToken  # noqa: E402
from taifeng.skill.recall import SkillCandidate  # noqa: E402
from taifeng.skill.verify import LlmSkillVerifier  # noqa: E402

# 复用对抗 bench 的 skill 定义与树生成（同源，保证 body = 同一份「输入要求」富文本）
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bvv", str(HERE / "bench_verify_value.py")
)
_bvv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_bvv)  # type: ignore[union-attr]

# 第 1 跑被验证门误伤成 None 的 SAT 任务（取其对抗任务文案 + 正确 skill_id）。
# 只探被拦的 SAT——这些是「本该放行却被拦」的争议样本，最能区分 (A) 拦得对 vs (B) 拦得过苛。
_REJECTED_SAT = [
    "media-image-ocr",
    "media-audio-transcribe",
    "media-video-summarize",
    "data-csv-stats",
    "data-timeseries-forecast",
    "data-sql-tune",
    "gen-regex",
    "gen-color-palette",
]

# 模拟 router 写出的「能力关键词 query」（剥离了用户的输入上下文，符合 search_skills
# schema「避免照抄口语原句、用能力关键词+同义词」的指引）。用于对照：把验证器的输入
# 从「真任务」换成「关键词 query」，看适配判断是否翻转——复现 search_skills.py:206
# 传 query 而非原任务导致的过拦机制。
_ROUTER_QUERY = {
    "media-image-ocr": "图片 文字识别 OCR 提取文字 截图",
    "media-audio-transcribe": "音频 语音转写 录音转文字 转录",
    "media-video-summarize": "视频 内容概括 要点总结 摘要",
    "data-csv-stats": "表格 统计分析 描述统计 数据分布 均值",
    "data-timeseries-forecast": "时间序列 趋势预测 预测 未来值",
    "data-sql-tune": "SQL 性能优化 慢查询 执行计划 索引",
    "gen-regex": "正则表达式 模式匹配 文本提取 regex",
    "gen-color-palette": "配色方案 调色板 色彩搭配 主色",
}


def _body_of(tree: Path, skill_id: str) -> str:
    """取某 skill 的 body（去掉 frontmatter，与内核 get_body 给验证器的口径一致）。"""
    raw = (tree / skill_id / "SKILL.md").read_text(encoding="utf-8")
    # frontmatter 在第二个 '---' 之后是 body
    parts = raw.split("---", 2)
    return parts[2].strip() if len(parts) == 3 else raw


def _task_for(skill_id: str) -> str:
    """据正确 skill_id 找回它的 SAT 任务文案。"""
    for t in _bvv._TASKS:
        if t["kind"] == "sat" and t["expected"] == skill_id:
            return t["task"]
    raise KeyError(skill_id)


async def main() -> None:
    """对每个被拦 SAT，dump 验证器原始逐候选裁决（含 applicable=False + reason）。"""
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        raise SystemExit(f"❌ 无法构造真实 LLM client：{exc}") from None

    verifier = LlmSkillVerifier(client)
    cancel = CancellationToken()

    with tempfile.TemporaryDirectory() as td:
        tree = Path(td) / "skills"
        _bvv._build_tree(tree)

        print("=" * 72)
        print(f"验证门拒绝理由探针（真实 LLM · {meta['provider']}/{meta['model']}）")
        print("=" * 72)

        async def _verdict(skill_id: str, verifier_input: str) -> None:
            """对单候选用给定 verifier_input 跑一次验证，dump 原始裁决（含不适用 reason）。"""
            cand = SkillCandidate(
                skill_id=skill_id, description="", score=1.0,
                confidence=1.0, matched_snippet=None,
            )
            verifiable, bodies, truncated = verifier._collect_bodies(
                [cand], lambda sid: _body_of(tree, sid)
            )
            request = verifier._build_request(verifier_input, verifiable, bodies)
            answer = await verifier._call_llm(request, cancel)
            try:
                for item in json.loads(answer):
                    flag = "✅适用" if item.get("applicable") else "❌不适用"
                    print(f"     {flag} (conf={item.get('confidence')}) "
                          f"{item.get('reason')}")
            except json.JSONDecodeError:
                print(f"     [非 JSON 原文] {answer[:300]}")

        # ── Pass 1：喂验证器**真实任务**（含输入上下文）——看它本身是否过苛 ──
        print("\n【Pass 1】验证器输入 = 真实用户任务（含「附件是照片」等输入上下文）\n")
        for skill_id in _REJECTED_SAT:
            task = _task_for(skill_id)
            print(f"── {skill_id} ──\n   任务：{task}")
            await _verdict(skill_id, task)
            print()

        # ── Pass 2：喂验证器**关键词 query**（剥离输入上下文）——复现 search_skills 实况 ──
        print("\n【Pass 2】验证器输入 = router 关键词 query（剥离输入上下文，"
              "= search_skills.py:206 实际传给验证器的东西）\n")
        for skill_id in _REJECTED_SAT:
            q = _ROUTER_QUERY[skill_id]
            print(f"── {skill_id} ──\n   query：{q}")
            await _verdict(skill_id, q)
            print()


if __name__ == "__main__":
    asyncio.run(main())
