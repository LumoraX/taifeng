"""examples/ 冒烟核验 —— 按**输出内容**判定 sim 档示例是否真的跑通。

为什么不能只看退出码:绝大多数 demo 末尾无条件 ``return 0``,turn 失败照样退 0。
本脚本对每个示例三重判定,任一不满足即 FAIL:

  1. 退出码 == 0;
  2. 输出非空(跑完什么都没打印 = 可疑);
  3. 输出不含硬失败标记(见 ``HARD_MARKERS``)。

分档规则见 :func:`classify`(有序,先匹配先生效)。**未被任何跳过规则命中的文件
默认进 sim 档执行** —— 这是刻意的「安全默认」:漏分类让 CI 变红、逼人显式裁决,
而不是被静默跳过然后烂在那里(``examples/mcp_basic`` 曾因错误分档坏了很久没被发现)。

用法::

    python scripts/verify_examples.py                 # 执行 sim 档全部示例
    python scripts/verify_examples.py --list          # 只打印分档,不执行
    python scripts/verify_examples.py --log-dir DIR   # 每个示例的完整输出落盘
"""

from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 硬失败标记 —— 出现任意一条即判 FAIL。均取自真实事故(带内失败但退出码为 0)。
HARD_MARKERS = (
    r"Traceback \(most recent call last\)",
    r"turn_failed",
    r"isError=True",
    r"\bTURN ✗",
    r"❌",
    r"skill_unavailable",
    r"bad_response_status_code",
    r"SimContractViolation",
    r"unknown skill",
)

# 需真实 LLM key 的判据:示例是否引用 examples/_provider_bootstrap。
# 注意不能用「是否调 build_model_client」—— mcp_basic / mcp_hitl 通过环境变量
# 把 key 传给 spawn 出来的 `taifeng mcp serve` 子进程,并不直接构造 client。
KEY_MARKER = "_provider_bootstrap"

# 无法用模式表达的「非独立入口」,逐条显式登记(路径不存在即报错,防条目变陈旧)。
NOT_ENTRY: dict[str, str] = {
    "examples/mcp_showcase/mcp_server.py": "被 demo spawn 的 stdio 子进程,不独立运行",
    "examples/step_pipeline/pipeline.py": "纯库模块,由 demo/server 导入",
    "examples/real_llm/test_openai_image_matrix.py": "真实图片矩阵,自读 env key",
    "examples/real_llm/test_codex_image_matrix.py": "真实图片矩阵,自读 env key",
    "examples/real_llm/skill_select/build_skills.py": "基准数据生成器,产出未纳管的 skills/",
}


class Tier(enum.StrEnum):
    """示例分档 —— 决定跑不跑、不跑的理由是什么。"""

    RUN = "RUN"
    SKIP_HELPER = "SKIP/helper"
    SKIP_SKILL_SCRIPT = "SKIP/skill-script"
    SKIP_NEEDS_KEY = "SKIP/needs-key"
    SKIP_NOT_ENTRY = "SKIP/not-entry"


@dataclass(frozen=True)
class Verdict:
    """单个示例的核验结论。"""

    path: str
    tier: Tier
    reason: str
    code: object = None
    seconds: float = 0.0
    markers: tuple[str, ...] = ()
    chars: int = 0

    @property
    def failed(self) -> bool:
        """三重判定:退出码非 0 / 输出为空 / 命中硬失败标记。"""
        if self.tier is not Tier.RUN:
            return False
        return self.code != 0 or self.chars == 0 or bool(self.markers)


def classify(rel: str, source: str) -> tuple[Tier, str]:
    """给单个示例文件分档。规则有序,先匹配先生效。"""
    if "/skills/" in rel:
        return Tier.SKIP_SKILL_SCRIPT, "SKILL.md scripts,由运行时调用"
    name = Path(rel).name
    if name.startswith("_") or name.endswith("_lib.py"):
        return Tier.SKIP_HELPER, "下划线前缀 / _lib 后缀 = 库/辅助模块"
    if rel in NOT_ENTRY:
        return Tier.SKIP_NOT_ENTRY, NOT_ENTRY[rel]
    if KEY_MARKER in source:
        return Tier.SKIP_NEEDS_KEY, f"引用 {KEY_MARKER},需真实 LLM key"
    if name == "server.py":
        return Tier.SKIP_NOT_ENTRY, "常驻 HTTP 服务,不自行退出"
    return Tier.RUN, "sim 档"


def discover(repo_root: Path) -> list[tuple[str, Tier, str]]:
    """扫描 examples/ 下全部 .py 并分档;顺带校验 NOT_ENTRY 无陈旧条目。"""
    stale = [p for p in NOT_ENTRY if not (repo_root / p).exists()]
    if stale:
        raise SystemExit(f"NOT_ENTRY 含已不存在的路径(请清理):{stale}")
    rows: list[tuple[str, Tier, str]] = []
    for path in sorted((repo_root / "examples").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        tier, reason = classify(rel, path.read_text(encoding="utf-8", errors="replace"))
        rows.append((rel, tier, reason))
    return rows


def _child_env(repo_root: Path) -> dict[str, str]:
    """构造子进程环境:强制 src-layout 可导入,并剥掉真实 key 防误连端点。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["TERM"] = "dumb"
    for key in [k for k in env if k.startswith("LLM_BOOTSTRAP_")]:
        del env[key]
    return env


def run_one(
    rel: str, repo_root: Path, env: dict[str, str], log_dir: Path | None, timeout: float
) -> Verdict:
    """跑一个 sim 档示例,合并 stdout/stderr 后做三重判定。"""
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603 — 跑的是本仓库自己的示例脚本
            [sys.executable, rel],
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        blob, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        blob = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        code = "TIMEOUT"
    markers = tuple(sorted({pat for pat in HARD_MARKERS if re.search(pat, blob)}))
    if log_dir is not None:
        name = rel.replace("/", "__").removesuffix(".py")
        (log_dir / f"{name}.log").write_text(blob, encoding="utf-8")
    return Verdict(
        path=rel, tier=Tier.RUN, reason="sim 档", code=code,
        seconds=round(time.time() - started, 1),
        markers=markers, chars=len(blob.strip()),
    )


def main() -> int:
    """入口:分档 → 逐个执行 sim 档 → 汇总;有 FAIL 返回 1。"""
    parser = argparse.ArgumentParser(description="examples/ 冒烟核验")
    parser.add_argument("--list", action="store_true", help="只打印分档,不执行")
    parser.add_argument("--log-dir", type=Path, default=None, help="逐例输出落盘目录")
    parser.add_argument("--timeout", type=float, default=300.0, help="单例超时秒数")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    rows = discover(repo_root)
    if args.log_dir is not None:
        args.log_dir.mkdir(parents=True, exist_ok=True)

    for rel, tier, reason in rows:
        if tier is not Tier.RUN:
            print(f"{tier:18} {rel:<62} {reason}")
    skipped = sum(1 for _, tier, _ in rows if tier is not Tier.RUN)
    runnable = [rel for rel, tier, _ in rows if tier is Tier.RUN]
    print(f"\n分档:{len(runnable)} 个执行 / {skipped} 个跳过 / 共 {len(rows)} 个 .py\n")
    if args.list:
        for rel in runnable:
            print(f"{Tier.RUN:18} {rel}")
        return 0

    env = _child_env(repo_root)
    failures: list[Verdict] = []
    for rel in runnable:
        verdict = run_one(rel, repo_root, env, args.log_dir, args.timeout)
        tag = "FAIL" if verdict.failed else "ok"
        note = ",".join(verdict.markers) or ("空输出" if verdict.chars == 0 else "")
        print(f"{tag:5} code={str(verdict.code):>7} {verdict.seconds:6.1f}s "
              f"{rel:<62} {note}")
        if verdict.failed:
            failures.append(verdict)

    print(f"\n=== 通过 {len(runnable) - len(failures)}/{len(runnable)} ===")
    for verdict in failures:
        print(f"  ✗ {verdict.path}: code={verdict.code} "
              f"markers={verdict.markers} chars={verdict.chars}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
