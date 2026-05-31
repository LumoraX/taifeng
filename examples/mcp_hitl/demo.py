"""端到端 MCP server HITL 验证脚本 —— 完整闭环演示 elicitation/create。

演示 G1 mcp-server-hitl-elicitation 完整链路：

    LLM 触发 tool 调用 → PermissionPolicy.check(ask)
      → McpPrompter.prompt(request)
      → server.server_initiated_request("elicitation/create", ...)
      → stdout 写出反向 RPC（host 可读）
    ← host 决策（accept/deny）写回 stdin
      ← McpPrompter parse → PermissionDecision
      → tool 执行或拒绝

本脚本扮演 host 角色：

    - 父进程 spawn ``taifeng mcp serve ... --enable-hitl``
    - 同时监听子进程 stdout：识别 server-initiated request → 模拟用户决策
    - 调一次 tools/call run_skill_turn，让 server 内部触发 PermissionPolicy.check

运行（用 mock LLM 走真实 dispatch；不依赖外部 API）：

    cd taifeng
    PYTHONPATH=src uv run python examples/mcp_server_hitl_e2e.py

输出中你会看到：

    ← 收到 server-initiated request: elicitation/create  id=srv_1
       message: Approve operation? ...
       → 模拟 user: ✓ approved

    并最终拿到完整 turn 输出。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SKILLS_DIR = HERE / "skills"

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
sys.path.insert(0, str(HERE.parent))
from _provider_bootstrap import load_dotenv_files  # noqa: E402


class HostSimulator:
    """模拟 MCP host：处理 server-initiated requests + 客户端 requests。

    扮演的角色：
        - 收到 server 主动发的 elicitation/create → 决策 + 回包
        - 主动发 tools/call → 等 server response
    """

    def __init__(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        *,
        approve: bool = True,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._approve = approve
        self._next_id = 1
        # 父进程发起的 request id → future（等 server response）
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

    async def _read_loop(self) -> None:
        """持续读 server stdout，分流：response → resolve future；
        server-initiated request → 模拟决策回包。"""
        while True:
            raw = await self._stdout.readline()
            if not raw:
                return
            text = raw.decode("utf-8").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue

            req_id = msg.get("id")
            method = msg.get("method")

            # 路径 1: server response（对父进程之前发的请求的回包）
            if isinstance(req_id, int) and (
                "result" in msg or "error" in msg
            ):
                fut = self._pending.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_result(msg)
                continue

            # 路径 2: server-initiated request（如 elicitation/create）
            if method == "elicitation/create":
                params = msg.get("params") or {}
                print(
                    f"\n  ← 收到 server-initiated request: {method}  "
                    f"id={req_id}",
                    flush=True,
                )
                message = params.get("message", "")
                for line in message.splitlines():
                    print(f"     {line}", flush=True)

                decision_word = "✓ approved" if self._approve else "✗ denied"
                print(f"     → 模拟 user: {decision_word}", flush=True)

                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "action": "accept",
                        "content": {
                            "approved": self._approve,
                            "reason": "demo decision",
                        },
                    },
                }
                line_out = (
                    json.dumps(response, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                self._stdin.write(line_out)
                await self._stdin.drain()
                continue

            # 其他 server-initiated 方法（暂不支持）
            print(f"  ← unknown server message: method={method} id={req_id}",
                  flush=True)

    async def call(
        self, method: str, params: dict[str, Any] | None = None,
        *, timeout_s: float = 120.0,   # noqa: ASYNC109
    ) -> dict[str, Any]:
        """父进程发起 JSON-RPC 请求并等 server response。"""
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut

        payload = {
            "jsonrpc": "2.0", "id": req_id,
            "method": method, "params": params or {},
        }
        arrow = json.dumps(params or {}, ensure_ascii=False)
        print(f"\n  ➜ {method}({arrow[:80]})", flush=True)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._stdin.write(line)
        await self._stdin.drain()

        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            self._pending.pop(req_id, None)


async def main() -> int:
    load_dotenv_files()
    api_key = (
        os.environ.get("LLM_BOOTSTRAP_API_KEY")
        or os.environ.get("LLM_BOOTSTRAP_OPENAI_API_KEY")
    )
    if not api_key:
        print(
            "⚠ 未找到 LLM_BOOTSTRAP_API_KEY；HITL 仍可演示但 LLM 会失败。",
            flush=True,
        )

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

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["OPENAI_API_KEY"] = api_key or "missing"
    if base_url != "<default>":
        env["OPENAI_API_BASE"] = base_url

    litellm_model = f"openai/{model}"
    cmd = [
        sys.executable, "-m", "taifeng", "mcp", "serve",
        str(SKILLS_DIR),
        "--storage", str(storage_dir),
        "--model", litellm_model,
        "--enable-hitl",            # ← G1 关键 flag
        "--hitl-timeout", "30",
    ]
    print(f"[spawn] {' '.join(cmd[:6])} ... --enable-hitl", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.stdin and proc.stdout and proc.stderr

    # 模拟 host：所有 elicitation 自动批准
    host = HostSimulator(proc.stdin, proc.stdout, approve=True)
    host.start()

    try:
        print("\n=== 1) initialize ===", flush=True)
        resp = await host.call(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}},
        )
        info = resp.get("result", {})
        print(f"     server: {info.get('serverInfo')}", flush=True)

        print("\n=== 2) tools/call run_skill_turn (HITL: PermissionPolicy=ask) ===",
              flush=True)
        # 用 ``programmer`` 作 entry：LLM 几乎必然调 call_skill("code-review")
        # → PermissionPolicy.check(skill_dispatch) → ask → McpPrompter 反向
        # elicitation/create → 本脚本的 host simulator 自动 approve
        # （PermissionPolicy 当前覆盖的 scope：call_skill / run_script，
        # 其余 tool 走 PreToolUse hook；本 demo 聚焦 skill_dispatch 链路）
        message = (
            "请审查这段代码：\n\n"
            "def login(username, password):\n"
            "    q = f\"SELECT * FROM users WHERE name='{username}' \"\n"
            "        f\"AND pwd='{password}'\"\n"
            "    return db.execute(q).fetchone()"
        )
        print("     message: <multi-line code review request>", flush=True)
        resp = await host.call(
            "tools/call",
            {
                "name": "run_skill_turn",
                "arguments": {
                    "skill_id": "programmer",
                    "message": message,
                    "session_id": "hitl-demo",
                },
            },
            timeout_s=180.0,
        )
        result = resp.get("result", {})
        text = (result.get("content") or [{}])[0].get("text", "")
        is_error = result.get("isError", False)
        marker = "❌" if is_error else "✅"
        print(f"\n  ← isError={is_error} {marker}", flush=True)
        print(f"  ← final text:\n{text}\n", flush=True)

        print("=== ✓ HITL e2e 完成 ===", flush=True)

    finally:
        await host.close()
        with contextlib.suppress(Exception):
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        if err.strip():
            lines = [
                line for line in err.splitlines()
                if "ERROR" in line or "Traceback" in line
            ]
            if lines:
                print("\n[stderr (filtered)]\n  " + "\n  ".join(lines[:20]))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
