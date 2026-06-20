"""真实 LLM「验证门**真价值**」对抗基准 —— 富 body（写明输入要求）下 A/B 召回 vs 召回+验证。

为什么要有这个脚本（与 ``bench_search.py`` 的区别）：
- ``bench_search.py`` 的叶子 body == description（薄），验证门拉完整 body 也只看到描述，
  **无输入要求可判** → 实测验证对准确率中性（90.0% ≈ 仅召回 90.5%），证明不了自己。
- 本脚本给每个 skill 的 body **补真实「输入要求」段**，并造三类对抗任务，专测验证门设计目的：
  **「描述像、但当前任务的输入要求不满足」时，把误召剪掉**。

三类任务（每条带 expected，可为某 skill_id 或 None）：
- **SAT（满足）**：任务给足了该 skill 要求的输入 → expected = 该 skill。验证应**放行**（不误伤）。
- **MIS（缺输入）**：话题/描述贴该 skill，但**缺其要求的输入** → expected = None。
  仅召回会按描述误选该 skill；召回+验证应判 no_match → 不勉强 call_skill（None 才对）。
- **REDIRECT（改选兄弟）**：翻译对——doc 版要文件、text 版要纯文本，描述都像「翻译」；
  任务只给纯文本 → expected = text 版。仅召回易按描述选 doc 版；验证应**改选**对的兄弟。

A/B：同一批任务跑两遍——``verifier=None``（仅召回）与 ``verifier=LlmSkillVerifier``（召回+验证），
其余配置一致。报告分 SAT/MIS/REDIRECT 三类对比，看验证门**净收益**（救对 MIS/REDIRECT - 误伤 SAT）。

运行（需真实 LLM key，复用 examples/_provider_bootstrap 的 LLM_BOOTSTRAP_* env）：
    PYTHONPATH=src python examples/real_llm/skill_select/bench_verify_value.py

⚠️ 只跑真实 LLM；CI / 自检禁跑。结果数值由集成者真实跑后填入 RESULTS_SEARCH.md（**严禁造假**）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # examples/ 进 sys.path 取共享 bootstrap
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.permission.policy import PermissionPolicy  # noqa: E402
from taifeng.skill.recall import KeywordSkillRecall  # noqa: E402
from taifeng.skill.verify import LlmSkillVerifier  # noqa: E402

# ── 对抗 skill 集：每个 body 都有「## 输入要求」段，供验证门拉完整 body 判适配 ──
# 字段：id / name / desc（进 frontmatter，供召回按描述匹配）/ require（输入要求，进 body）。
# 刻意让同组描述相近（媒体理解 / 数据分析 / 生成构造 / 翻译对），逼召回靠描述、验证靠输入要求区分。
_SKILLS: list[dict[str, str]] = [
    # —— 媒体内容理解组（描述都像「理解…内容」，输入要求各异）——
    {"id": "media-image-ocr", "name": "图片文字识别",
     "desc": "从图片中识别并提取其中的文字内容（OCR）",
     "require": "**一张或多张图片文件**（PNG/JPG/截图/拍照）。若任务未提供任何图片，仅有口述或文字，则本技能不适用。"},
    {"id": "media-audio-transcribe", "name": "语音转写",
     "desc": "将语音音频转写为文字稿",
     "require": "**一段音频文件**（MP3/WAV/语音消息/录音）。若任务给的已经是文字、没有任何音频，则本技能不适用。"},
    {"id": "media-video-summarize", "name": "视频内容概括",
     "desc": "概括视频讲解的内容要点",
     "require": "**视频文件本身或其字幕/转录轨**。若任务只凭记忆口述「视频大概讲了啥」、未提供视频或字幕，则本技能不适用。"},
    # —— 数据分析组（描述都像「分析…数据」，输入要求各异）——
    {"id": "data-csv-stats", "name": "表格统计分析",
     "desc": "对表格数据做描述性统计与分布分析",
     "require": "**结构化表格数据**（CSV/Excel，含表头列名与数值行）。若任务只有自然语言叙述、没有可解析的表格，则本技能不适用。"},
    {"id": "data-timeseries-forecast", "name": "时间序列预测",
     "desc": "基于历史数值序列做未来趋势预测",
     "require": "**一组带时间戳的历史数值序列**（至少十几个数据点）。若任务没有历史数据点、要求凭感觉估，则本技能不适用。"},
    {"id": "data-sql-tune", "name": "SQL 性能优化",
     "desc": "分析并优化慢 SQL 查询的执行性能",
     "require": "**一条具体的 SQL 语句文本 + 相关表结构/索引信息**。若任务只笼统说「查询慢」、给不出 SQL，则本技能不适用。"},
    # —— 生成/构造组 ——
    {"id": "gen-regex", "name": "正则表达式构造",
     "desc": "根据需求构造正则表达式",
     "require": "**待匹配的示例文本 + 明确的匹配目标描述**。若任务说不清要匹配什么、也无示例，则本技能不适用。"},
    {"id": "gen-color-palette", "name": "配色方案生成",
     "desc": "生成协调的配色方案",
     "require": "**一个主色（hex 值）或一张参考图**作为基准。若任务未指定任何主色或参考，则本技能不适用。"},
    # —— 翻译对（REDIRECT 用：描述都像「翻译」，输入类型不同）——
    {"id": "gen-translate-doc", "name": "文档翻译",
     "desc": "翻译上传的文档文件",
     "require": "**一个上传的文档文件**（.docx/.pdf/.txt 附件）。若任务只是直接粘贴的一两句纯文本、没有文件附件，则应改用「纯文本翻译」，本技能不适用。"},
    {"id": "gen-translate-text", "name": "纯文本翻译",
     "desc": "翻译直接粘贴的文本",
     "require": "**直接给出的文本字符串**即可。"},
]

# ── 对抗任务：kind ∈ {sat, mis, redirect}；expected 为 skill_id 或 None ──
_TASKS: list[dict] = [
    # SAT：给足输入，验证应放行 → expected = 该 skill
    {"kind": "sat", "expected": "media-image-ocr",
     "task": "附件是一张发票的照片，帮我把上面的金额和开票日期这些文字提取出来。"},
    {"kind": "sat", "expected": "media-audio-transcribe",
     "task": "这是一段三分钟的会议录音 mp3 文件，帮我转成文字稿。"},
    {"kind": "sat", "expected": "media-video-summarize",
     "task": "我上传了一个十分钟的产品讲解视频，帮我总结里面的要点。"},
    {"kind": "sat", "expected": "data-csv-stats",
     "task": "这是销售明细 CSV，有日期、地区、金额三列共两千行，帮我算各地区均值和金额分布。"},
    {"kind": "sat", "expected": "data-timeseries-forecast",
     "task": "这是过去 60 天每天的访问量数字序列 [1203, 1180, ...]，预测下周每天大概多少。"},
    {"kind": "sat", "expected": "data-sql-tune",
     "task": "SELECT * FROM orders WHERE status=1 ORDER BY created_at 这条 SQL 很慢，orders 表 500 万行、created_at 无索引，帮我优化。"},
    {"kind": "sat", "expected": "gen-regex",
     "task": "示例文本是『订单号 A1234-9 已发货』，我想用正则把订单号 A1234-9 这种抓出来，给我表达式。"},
    {"kind": "sat", "expected": "gen-color-palette",
     "task": "我们品牌主色是 #2E7D32，帮我配一套协调的辅助色和点缀色。"},
    {"kind": "sat", "expected": "gen-translate-doc",
     "task": "附件这个 report.docx 文档，帮我整篇翻译成英文。"},
    {"kind": "sat", "expected": "gen-translate-text",
     "task": "把这句话翻译成英文：『今天的天气很好，适合散步』。"},
    # MIS：话题贴该 skill 但缺其要求输入 → expected = None（验证应判 no_match、不勉强调用）
    {"kind": "mis", "expected": None,
     "task": "我想识别图片上的文字，不过图我还没拍，我口头描述一下上面写了『欢迎光临』，你帮我整理成文字。"},
    {"kind": "mis", "expected": None,
     "task": "帮我把今天这场会议转成文字纪要——内容是我现在打字告诉你：先过了上周进度，然后定了下周排期。"},
    {"kind": "mis", "expected": None,
     "task": "帮我总结一下那个装机视频讲了啥，视频我自己看过了，大概就是讲怎么插内存和硬盘。"},
    {"kind": "mis", "expected": None,
     "task": "帮我统计分析一下我们上个月的销售情况，具体数字我没带，我印象里华东卖得最好。"},
    {"kind": "mis", "expected": None,
     "task": "预测一下我们网站下周的访问量大概多少，我没有历史数据，你凭经验估个数就行。"},
    {"kind": "mis", "expected": None,
     "task": "我的数据库最近查询很慢，具体是哪条 SQL 我也说不上来，反正就是查订单那块卡。"},
    {"kind": "mis", "expected": None,
     "task": "帮我写个正则吧，具体要匹配什么我也说不太清，你看着办给一个通用的。"},
    {"kind": "mis", "expected": None,
     "task": "帮我生成一套好看的配色方案，主色什么的我没想好，你随便给点顺眼的就行。"},
    # REDIRECT：描述都像「翻译」，但只给了纯文本（无文件）→ expected = 纯文本翻译（验证应改选兄弟）
    {"kind": "redirect", "expected": "gen-translate-text",
     "task": "帮我把下面这段文字翻译成中文：『Hello world, nice to meet you』。"},
    {"kind": "redirect", "expected": "gen-translate-text",
     "task": "这句英文帮我翻成中文：『The quick brown fox jumps over the lazy dog』。"},
]

# 召回版 router：先搜后调；**关键新增**——多次召回均 no_match 时允许放弃不调（MIS 的正确行为）。
_ROUTER_BODY = """---
name: 技能路由器（验证价值版）
description: 顶层入口，先 search_skills 召回，再从召回结果中选唯一最匹配的调用；输入要求不满足时不勉强调用
version: 1.0.0
type: composite
entry: true
max_call_depth: 3
child_skills: [{children}]
---
# 技能路由器（验证价值版）

[Context 背景] 你面对很多子技能，它们未列在 prompt 里，需用 search_skills 主动检索发现。
每个技能不仅有能力描述，还有**输入要求**；只有当前任务真正提供了它要求的输入，调用才有意义。

[Role 角色] 你是技能路由器兼语义检索专家：把口语意图翻译成检索关键词，并在召回候选中
判断哪个技能**既贴合意图、又满足其输入要求**。

[Execute 执行指令] 为用户当前请求，找出并调用唯一最匹配且输入要求被满足的子技能；
若没有任何技能的输入要求被满足，则**不要勉强调用**。

[Action 动作步骤]
1. 拆意图：判断用户要做什么、属哪类能力，写 3-5 个能力关键词 + 同义词（不要复述原话）。
2. 召回：调用 `search_skills`，`query` = 上述关键词拼接。
3. 评估+反思：读召回候选；有贴切且输入要求满足的 → 进第 4 步；
   召回返回 no_match（无输入要求匹配的候选），或都不贴切 → 换一组关键词回第 2 步（最多 3 次）。
4. 决策：
   - 若找到合适候选 → 立即 `call_skill`（`skill_id` = 选中 id，`args` = {{}}）。
   - 若多轮召回后仍 no_match / 没有输入要求被满足的候选 → **不要 call_skill**，
     直接用一句话说明缺少哪类输入即可（这本身是正确行为，不要硬凑一个技能）。

[Target 目标与格式] 要么恰好一次 `call_skill` 调用真正适配的技能，要么明确说明因缺输入而不调用；
不解释多余内容、不寒暄。
"""

_LEAF_TMPL = """---
name: "{name}"
description: "{desc}"
version: 1.0.0
type: atomic
---
# {name}

{desc}

## 输入要求

{require}

> 验证价值对抗基准叶子：被路由器选中即代表「该任务应由我处理」。叶子不实际执行
> （bench 用权限拒绝拦在 call_skill，只看路由器选了谁 / 是否正确放弃）。
"""


def _build_tree(dest: Path) -> list[str]:
    """在 dest 下生成富 body 对抗 skills 树（10 叶子 + 1 router），返回叶子 id 列表。"""
    ids = [s["id"] for s in _SKILLS]
    for s in _SKILLS:
        d = dest / s["id"]
        d.mkdir(parents=True, exist_ok=True)
        body = _LEAF_TMPL.format(
            name=s["name"].replace('"', '\\"'),
            desc=s["desc"].replace('"', '\\"'),
            require=s["require"],
        )
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    rd = dest / "router"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "SKILL.md").write_text(
        _ROUTER_BODY.format(children=", ".join(ids)), encoding="utf-8"
    )
    return ids


async def _select_one(
    pool: object, idx: int, task: str, timeout: float
) -> tuple[str | None, int, bool]:
    """提交一条 task，返回 (最终 call_skill 的 skill_id 或 None, 验证滤除候选累计, 是否出现 no_match)。

    机制确认用：验证门每次 verify 后 emit ``skill_candidates_verified``（带 dropped_count /
    verified_count），据此判断「7 个 SAT→None」是验证过度拒绝(dropped>0 且 no_match)还是别的原因。
    """
    engine = await pool.get_or_create(  # type: ignore[attr-defined]
        session_id=f"vv-{idx}", entry_skill_id="router"
    )
    selected: str | None = None
    dropped = 0
    no_match = False
    done = asyncio.Event()

    async def watch() -> None:
        nonlocal selected, dropped, no_match
        async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
            kind = ev.msg.kind
            if kind == "skill_candidates_verified":
                dropped += int(ev.msg.data.get("dropped_count", 0))
                if int(ev.msg.data.get("verified_count", 0)) == 0:
                    no_match = True
            elif kind == "tool_call_started" and ev.msg.data.get("name") == "call_skill":
                try:
                    selected = json.loads(
                        ev.msg.data.get("arguments", "{}")
                    ).get("skill_id")
                except json.JSONDecodeError:
                    selected = None
                done.set()
                return
            if kind in ("turn_completed", "turn_failed"):
                # 没 call_skill 就终态 = 主动放弃 / 只输出文字 → None
                done.set()
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text=task))  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        watcher.cancel()
    return selected, dropped, no_match


def _score(got: str | None, expected: str | None) -> bool:
    """命中判定：expected 为 None 时要求 got 也为 None（正确放弃）；否则要求 id 相等。"""
    return got == expected


async def _run_mode(
    *, verify: bool, client: object, policy: object, timeout: float
) -> list[tuple[dict, str | None, int, bool]]:
    """跑一遍全部任务，返回 [(task_dict, got, dropped, no_match), ...]。verify=True 注入 LlmSkillVerifier。"""
    verifier = LlmSkillVerifier(client) if verify else None  # type: ignore[arg-type]
    out: list[tuple[dict, str | None, int, bool]] = []
    with tempfile.TemporaryDirectory() as td:
        skills_dir = Path(td) / "skills"
        _build_tree(skills_dir)
        pool = await taifeng.EnginePool.create(
            skills_dir=skills_dir,
            threads_dir=Path(td) / "threads",
            model_client=client,
            budget=ContextBudget(context_window=128_000),
            compressors=[],
            permission_policy=policy,
            skill_recall=KeywordSkillRecall(),
            skill_verifier=verifier,
            recall_threshold=0,   # 强制 deferred 召回
            max_iterations=5,     # 留重搜空间
        )
        try:
            for i, t in enumerate(_TASKS):
                got, dropped, no_match = await _select_one(
                    pool, i, t["task"], timeout
                )
                out.append((t, got, dropped, no_match))
        finally:
            await pool.close()
    return out


def _report(
    label: str, rows: list[tuple[dict, str | None, int, bool]], *, show_verify: bool
) -> dict[str, tuple[int, int]]:
    """打印某模式分类准确率，返回 {kind: (correct, total)}；show_verify 时附验证滤除/无匹配。"""
    agg: dict[str, list[int]] = {"sat": [0, 0], "mis": [0, 0], "redirect": [0, 0]}
    print(f"\n── {label} ──")
    for t, got, dropped, no_match in rows:
        k = t["kind"]
        ok = _score(got, t["expected"])
        agg[k][0] += int(ok)
        agg[k][1] += 1
        mark = "✓" if ok else "✗"
        exp = t["expected"] if t["expected"] is not None else "None(应放弃)"
        vtag = (f"  滤{dropped}{'·no_match' if no_match else ''}"
                if show_verify else "")
        print(f"  {mark} [{k:<8}] 期望={str(exp):<22} 实选={got}{vtag}")
    print(f"  小计 SAT {agg['sat'][0]}/{agg['sat'][1]} | "
          f"MIS {agg['mis'][0]}/{agg['mis'][1]} | "
          f"REDIRECT {agg['redirect'][0]}/{agg['redirect'][1]}")
    return {k: (v[0], v[1]) for k, v in agg.items()}


async def main() -> None:
    """跑验证门真价值 A/B（仅召回 vs 召回+验证），打印三类对比。"""
    ap = argparse.ArgumentParser(description="真实 LLM 验证门真价值对抗基准（A/B）")
    ap.add_argument("--timeout", type=float, default=150.0, help="单任务超时秒")
    ap.add_argument("--only", choices=["recall", "verify"], default=None,
                    help="只跑一个模式（默认两个都跑做 A/B）")
    args = ap.parse_args()

    try:
        client, meta = build_model_client(timeout_seconds=150.0)
    except ProviderBootstrapError as exc:
        raise SystemExit(f"❌ 无法构造真实 LLM client：{exc}") from None

    policy = PermissionPolicy.from_dict(
        {"default_mode": "allow", "deny": ["Skill(*)"]}
    )

    print("=" * 72)
    print("验证门真价值对抗基准（真实 LLM · 富 body 含输入要求 · A/B）")
    print("=" * 72)
    print(f"skill 池: {len(_SKILLS)} 个（含输入要求段） | 任务: {len(_TASKS)} 条"
          f"（SAT/MIS/REDIRECT）| provider={meta['provider']} model={meta['model']}")

    started = time.monotonic()
    rec_agg = ver_agg = None
    if args.only in (None, "recall"):
        rows = await _run_mode(verify=False, client=client, policy=policy,
                               timeout=args.timeout)
        rec_agg = _report("模式 A：仅召回（无验证门）", rows, show_verify=False)
    if args.only in (None, "verify"):
        rows = await _run_mode(verify=True, client=client, policy=policy,
                               timeout=args.timeout)
        ver_agg = _report("模式 B：召回 + 验证门", rows, show_verify=True)
    elapsed = time.monotonic() - started

    if rec_agg and ver_agg:
        print("\n" + "=" * 72)
        print("A/B 对比（correct/total，越高越好）")
        print(f"  {'类别':<10}{'仅召回':>14}{'召回+验证':>16}{'Δ':>8}")
        print("  " + "-" * 46)
        for k in ("sat", "mis", "redirect"):
            a, ta = rec_agg[k]
            b, tb = ver_agg[k]
            print(f"  {k:<10}{f'{a}/{ta}':>14}{f'{b}/{tb}':>16}{b - a:>+8}")
        ra = sum(rec_agg[k][0] for k in rec_agg)
        rb = sum(ver_agg[k][0] for k in ver_agg)
        tot = sum(rec_agg[k][1] for k in rec_agg)
        print("  " + "-" * 46)
        print(f"  {'总计':<10}{f'{ra}/{tot}':>14}{f'{rb}/{tot}':>16}{rb - ra:>+8}")
        print("\n解读：MIS/REDIRECT 的 Δ>0 = 验证门把误召救成正确；"
              "SAT 的 Δ<0 = 验证门误伤放行。净收益看总计 Δ。")
    print(f"\n耗时 {elapsed:.1f}s")
    print("🎯 验证门真价值对抗基准完毕")


if __name__ == "__main__":
    asyncio.run(main())
