"""真实 LLM 验证台账生成器 —— capability_matrix 跑测结果落盘。

双格式（D1）：
- ``docs/real-llm-ledger.json`` 机读真相：run 元信息 + 逐场景结果 + R3 审计；
- ``docs/real-llm-ledger.md``   人读渲染：由 json 单向生成，**勿手编辑**。

增量合并（支持 ``--only`` 单场景复跑）：本次未跑的场景保留上次结果，
渲染时按「场景 commit ≠ 本次 run commit」标注 stale。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# 台账固定落仓库 docs/（examples/real_llm/_ledger.py → parents[2] = 仓库根）
_REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = _REPO_ROOT / "docs"
LEDGER_JSON = DOCS_DIR / "real-llm-ledger.json"
LEDGER_MD = DOCS_DIR / "real-llm-ledger.md"


def git_short_commit() -> str:
    """当前 HEAD 短 hash；取不到（非 git 环境）返回 'unknown'。"""
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        out = subprocess.run(  # noqa: S603 —— 固定参数列表，无不可信输入
            [git, "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 —— 台账元信息缺失不应阻断跑测落盘
        return "unknown"


@dataclass
class ScenarioRecord:
    """单场景一次真实跑测的台账条目。"""

    scenario_id: str
    capability: str
    verdict: str               # PASS / PART / FAIL
    note: str                  # 失败原因 / 缺失事件说明（PASS 为空）
    expect: list[str]          # 期望关键事件
    missing: list[str]         # 未命中的期望事件
    kinds: dict[str, int]      # 全部事件 kind 计数
    grants: int                # HITL 自动放行次数
    duration_s: float          # 场景 wall-clock
    commit: str                # 本条结果产生时的 HEAD
    timestamp_utc: str         # 本条结果产生时间

    @property
    def passed(self) -> bool:
        """通过 = verdict 为 PASS（PART/FAIL 均不算，judgment 如实）。"""
        return self.verdict == "PASS"


@dataclass
class R3Audit:
    """R3 可观测完整性审计结论。"""

    emitted_kinds: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)        # 无专用 console 渲染
    canonical_missing: list[str] = field(default_factory=list)  # 经典事件未触发


class LedgerWriter:
    """台账写盘器：读旧 json → 合并本次结果 → 原子写 json + 渲染 md。"""

    def __init__(self, *, json_path: Path = LEDGER_JSON, md_path: Path = LEDGER_MD) -> None:
        self._json_path = json_path
        self._md_path = md_path

    def merge_and_write(
        self,
        *,
        provider: str,
        model: str,
        records: list[ScenarioRecord],
        r3: R3Audit,
        full_run: bool = True,
    ) -> tuple[Path, Path]:
        """合并本次结果并落盘；返回 (json_path, md_path)。

        合并规则：本次跑过的场景覆盖旧条目；未跑的保留旧条目原样
        （其 commit / 时间不变，渲染时与本次 run commit 不同即标 stale）。
        ``full_run=False``（--only 部分跑）时 **不覆盖** r3_audit——部分场景的
        事件面远小于全量，覆盖会把全量审计结论冲掉。
        """
        run_commit = git_short_commit()
        run_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        old_scenarios: dict[str, dict] = {}
        old_audit: dict | None = None
        not_executed: dict[str, dict] = {}
        if self._json_path.is_file():
            try:
                old_data = json.loads(self._json_path.read_text(encoding="utf-8"))
                old_scenarios = old_data.get("scenarios", {})
                old_audit = old_data.get("r3_audit")
                not_executed = old_data.get("not_executed", {})
            except (json.JSONDecodeError, OSError):
                # 旧台账损坏：如实丢弃重建（台账可由重跑再生，不做修补猜测）
                old_scenarios = {}

        merged = dict(old_scenarios)
        for rec in records:
            merged[rec.scenario_id] = asdict(rec)
        if any(rec.scenario_id.startswith("openai_") and "_image_" in rec.scenario_id
               for rec in records):
            not_executed.pop("openai_image_input", None)

        data = {
            "run": {
                "timestamp_utc": run_ts,
                "commit": run_commit,
                "provider": provider,
                "model": model,
                "scenarios_run": [r.scenario_id for r in records],
            },
            "scenarios": dict(sorted(merged.items())),
            "not_executed": dict(sorted(not_executed.items())),
            # 部分跑（--only）保留上次全量审计；无旧审计时仍记本次（聊胜于无）
            "r3_audit": asdict(r3) if full_run or old_audit is None else old_audit,
        }

        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._json_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(self._md_path, _render_md(data))
        return self._json_path, self._md_path

    def mark_not_executed(
        self,
        *,
        key: str,
        reason: str,
        command: str,
    ) -> tuple[Path, Path]:
        """在不改写旧真实结果的前提下登记一个当前验证缺口。"""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if self._json_path.is_file():
            data = json.loads(self._json_path.read_text(encoding="utf-8"))
        else:
            data = {
                "run": {
                    "timestamp_utc": timestamp,
                    "commit": git_short_commit(),
                    "provider": "not-executed",
                    "model": "not-executed",
                    "scenarios_run": [],
                },
                "scenarios": {},
                "r3_audit": asdict(R3Audit()),
            }
        gaps = data.setdefault("not_executed", {})
        gaps[key] = {
            "status": "NOT_EXECUTED",
            "reason": reason,
            "command": command,
            "commit": git_short_commit(),
            "timestamp_utc": timestamp,
        }
        data["not_executed"] = dict(sorted(gaps.items()))
        _atomic_write(self._json_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(self._md_path, _render_md(data))
        return self._json_path, self._md_path


def _atomic_write(path: Path, content: str) -> None:
    """tmp + replace 原子写，避免跑测中断留半截文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _render_md(data: dict) -> str:
    """由 json 数据渲染人读台账。"""
    run = data["run"]
    lines = [
        "# 真实 LLM 验证台账",
        "",
        "> **本文件由 `examples/real_llm/capability_matrix.py` 自动生成（数据源 "
        "`real-llm-ledger.json`），勿手编辑。**",
        "> 回归红线：基础层（`src/taifeng/{llm,loop,context,conversation}/`）变更必须"
        "全量重跑并提交本台账；详见 CLAUDE.md §测试约束。",
        "",
        f"- **最近一次回归**：{run['timestamp_utc']} @ `{run['commit']}`",
        f"- **Provider / Model**：{run['provider']} / {run['model']}",
        f"- **本次跑测场景**：{', '.join(run['scenarios_run']) or '（无）'}",
        "",
        "## 逐场景结果",
        "",
        "| 场景 | 能力 | 结果 | 日期 @ commit | 耗时 | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    icon = {"PASS": "✅", "PART": "⚠️", "FAIL": "❌"}
    for sid, sc in data["scenarios"].items():
        stale = "（stale）" if sc["commit"] != run["commit"] else ""
        note = sc["note"] or ""
        if sc["missing"]:
            note = (note + " " if note else "") + f"缺事件: {sc['missing']}"
        lines.append(
            f"| `{sid}` | {sc['capability']} | {icon.get(sc['verdict'], '?')}{sc['verdict']} "
            f"| {sc['timestamp_utc'][:10]} @ `{sc['commit']}`{stale} "
            f"| {sc['duration_s']:.0f}s | {note} |"
        )

    r3 = data["r3_audit"]
    gaps = data.get("not_executed", {})
    if gaps:
        lines += ["", "## 未执行验证", ""]
        for key, gap in gaps.items():
            lines.append(
                f"- **{key}**：`{gap['status']}` — {gap['reason']} "
                f"（`{gap['command']}`，{gap['timestamp_utc']} @ `{gap['commit']}`）"
            )
    lines += [
        "",
        "## R3 可观测完整性审计（最近一次全量）",
        "",
        f"- 发出的事件 kind：{len(r3['emitted_kinds'])} 种",
        (f"- ⚠️ 无专用 console 渲染（落 `?` 兜底）：{r3['unmapped']}"
         if r3["unmapped"] else "- ✅ 所有发出的事件 kind 都有专用 console 渲染"),
        (f"- ℹ️ 经典事件本轮未触发：{r3['canonical_missing']}"
         if r3["canonical_missing"] else "- ✅ R3 经典事件全部触发"),
        "",
        "## 判定口径",
        "",
        "- **PASS** = 终态完成 ∧ 期望关键事件全命中；**PART** = 完成但缺关键事件；"
        "**FAIL** = turn_failed / 未完成 / 场景异常。",
        "- LLM 不配合（不调对应工具）如实记 FAIL/PART，不自动重试美化。",
        "- **stale** = 该场景结果产生于更早的 commit（本次未复跑）。",
        "",
    ]
    return "\n".join(lines)
