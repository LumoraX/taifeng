"""端到端 MCP server 验证脚本 —— spawn 子进程 + 走 5 步 JSON-RPC。

演示 server 模式下 LLM + skill loop 完整链路：

    1. 子进程跑 ``python -m taifeng mcp serve <skills> --storage <store>``
    2. 父进程通过 stdin/stdout JSON-RPC 与之对话：
       - initialize → handshake
       - tools/list → 看到 run_skill_turn meta-tool
       - resources/list → 看到所有 skill 作为资源
       - resources/read → 读 SKILL.md body
       - tools/call run_skill_turn → 真实 LLM 跑一轮 turn，server 内部触发
         read_skill / call_skill / 工具派发等所有事件
    3. 父进程打印完整请求 / 响应链路

运行（需 LLM_BOOTSTRAP_OPENAI_* 环境变量；从 .env 自动加载）：

    cd taifeng
    PYTHONPATH=src uv run python examples/mcp_server_e2e.py

预期输出：每步 JSON-RPC 的请求 + 响应 + 最后 LLM 的结构化答复。

参照：spec ``mcp-server`` Requirement
"CLI ``mcp serve`` 子命令" / ADR 实现 M3 + G1。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SKILLS_DIR = HERE / "skills"

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
sys.path.insert(0, str(HERE.parent))
from _provider_bootstrap import load_dotenv_files  # noqa: E402


async def jsonrpc_call(
    stdin: asyncio.StreamWriter,
    stdout: asyncio.StreamReader,
    method: str,
    params: dict | None = None,
    *,
    req_id: int,
    timeout_s: float = 30.0,   # noqa: ASYNC109 — JSON-RPC demo 用 wait_for 即可
) -> dict:
    """发一次 JSON-RPC 请求 + 等同 id 的 response。"""
    payload = {
        "jsonrpc": "2.0", "id": req_id,
        "method": method, "params": params or {},
    }
    arrow_params = json.dumps(params or {}, ensure_ascii=False)
    print(f"  ➜ {method}({arrow_params[:80]})", flush=True)
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    stdin.write(line)
    await stdin.drain()
    # 读到匹配 id 的 response
    while True:
        raw = await asyncio.wait_for(stdout.readline(), timeout=timeout_s)
        if not raw:
            raise RuntimeError("server closed stdout unexpectedly")
        try:
            resp = json.loads(raw.decode("utf-8").strip())
        except json.JSONDecodeError:
            continue
        # 跳过 server-initiated request（不带 result/error，本脚本不处理）
        if resp.get("id") == req_id:
            return resp


async def main() -> int:
    load_dotenv_files()

    # 直接读 env（MCP server 子进程会自己 import _provider_bootstrap
    # 走 native client；父进程这里只做参数透传 + 日志）
    api_key = (
        os.environ.get("LLM_BOOTSTRAP_API_KEY")
        or os.environ.get("LLM_BOOTSTRAP_OPENAI_API_KEY")
    )
    if not api_key:
        print(
            "❌ 缺少 LLM_BOOTSTRAP_API_KEY (或旧形态 "
            "LLM_BOOTSTRAP_OPENAI_API_KEY)；请配 .env。",
            flush=True,
        )
        print("   仍可继续以看 5 步 JSON-RPC 结构（最后一步会失败）。", flush=True)

    provider = (os.environ.get("LLM_BOOTSTRAP_PROVIDER") or "openai").lower()
    model = (
        os.environ.get("LLM_BOOTSTRAP_MODEL")
        or os.environ.get("LLM_BOOTSTRAP_OPENAI_MODEL")
        or "gpt-4o-mini"
    )
    base_url = (
        os.environ.get("LLM_BOOTSTRAP_BASE_URL")
        or os.environ.get("LLM_BOOTSTRAP_OPENAI_BASE_URL")
        or "<default>"
    )
    print(
        f"[setup] provider={provider}  model={model}  base_url={base_url}",
        flush=True,
    )
    print(f"[setup] skills={SKILLS_DIR}", flush=True)

    # 演示落盘统一收口到 .taifeng/examples/ 伞形目录，避免污染仓库根
    storage_dir = HERE / ".runs"
    storage_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] storage={storage_dir}", flush=True)

    # LiteLLM 走 OpenAI-compat endpoint：openai/<model>，base_url 由
    # OPENAI_API_BASE 注入（MCP server 子进程的 CLI 暂保持 litellm 兼容）
    litellm_model = f"openai/{model}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["OPENAI_API_KEY"] = api_key or "missing"
    if base_url != "<default>":
        env["OPENAI_API_BASE"] = base_url

    cmd = [
        sys.executable, "-m", "taifeng", "mcp", "serve",
        str(SKILLS_DIR),
        "--storage", str(storage_dir),
        "--model", litellm_model,
    ]
    print(f"[spawn] {' '.join(cmd[:6])} ...", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.stdin and proc.stdout and proc.stderr

    try:
        print("\n=== 1) initialize（协议 handshake） ===", flush=True)
        resp = await jsonrpc_call(
            proc.stdin, proc.stdout, "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}},
            req_id=1,
        )
        info = resp.get("result", {})
        print(f"  ← protocolVersion={info.get('protocolVersion')}", flush=True)
        print(f"  ← serverInfo={info.get('serverInfo')}", flush=True)

        print("\n=== 2) tools/list（看 meta-tool） ===", flush=True)
        resp = await jsonrpc_call(
            proc.stdin, proc.stdout, "tools/list", {}, req_id=2,
        )
        for t in resp.get("result", {}).get("tools", []):
            print(f"  • {t['name']}: {t['description'][:90]}...", flush=True)

        print("\n=== 3) resources/list（看 skill 作为资源） ===", flush=True)
        resp = await jsonrpc_call(
            proc.stdin, proc.stdout, "resources/list", {}, req_id=3,
        )
        for r in resp.get("result", {}).get("resources", []):
            print(f"  • {r['uri']}: {r['name']}", flush=True)

        print("\n=== 4) resources/read（读 SKILL.md body） ===", flush=True)
        resp = await jsonrpc_call(
            proc.stdin, proc.stdout, "resources/read",
            {"uri": "taifeng://skill/style-checker"},
            req_id=4,
        )
        contents = resp.get("result", {}).get("contents", [])
        if contents:
            preview = contents[0].get("text", "")[:160].replace("\n", " ")
            print(f"  ← {preview}...", flush=True)

        print("\n=== 5) tools/call run_skill_turn（真实 LLM 一轮） ===", flush=True)
        message = "user_service.py 第 45 行新增 130 行函数，含 SQL 拼接。极简告诉我规则编号和风险等级。"
        print(f"  ➜ message: {message}", flush=True)
        resp = await jsonrpc_call(
            proc.stdin, proc.stdout, "tools/call",
            {
                "name": "run_skill_turn",
                "arguments": {
                    "skill_id": "code-reviewer",
                    "message": message,
                    "session_id": "e2e-demo",
                },
            },
            req_id=5, timeout_s=240.0,
        )
        result = resp.get("result", {})
        is_error = result.get("isError", False)
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        marker = "❌" if is_error else "✅"
        print(f"  ← isError={is_error} {marker}", flush=True)
        print(f"  ← text:\n{text}\n", flush=True)

        print("=== ✓ MCP server e2e 完成 ===", flush=True)

    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            await asyncio.sleep(0.5)
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        if err.strip():
            # 默认只显示 ERROR / Traceback；环境变量 TAIFENG_DEMO_VERBOSE=1
            # 显示完整 stderr（含 litellm warnings + LLM trace）
            if os.environ.get("TAIFENG_DEMO_VERBOSE"):
                print("\n[stderr]\n" + err)
            else:
                lines = [
                    line for line in err.splitlines()
                    if "ERROR" in line or "Traceback" in line
                    or "Error" in line.lower().split(":")[0]
                    or "Exception" in line
                ]
                if lines:
                    print("\n[stderr (filtered)]\n  " + "\n  ".join(lines[:30]))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
