"""MCP stdio server —— 把 taifeng EnginePool 暴露给 MCP 客户端。

参照：
    - codex codex-rs/mcp-server
    - claw-code crates/runtime/src/mcp_server.rs
    - https://modelcontextprotocol.io / spec 2024-11-05

设计原则（R1 业务零侵入）：
    - McpStdioServer 不构造 EnginePool；业务侧先按既有路径配齐，再注入 server
    - server 是薄壳：JSON-RPC 协议 + 派发到 pool 已有 API
    - 不引入第三方 mcp-sdk / FastMCP；与 stdio_client.py 一致手写 JSON-RPC

支持的 MCP 方法（最小可用集）：
    - initialize（handshake）
    - tools/list, tools/call（暴露 `run_skill_turn` meta-tool）
    - resources/list, resources/read（每个 skill 一个 ``taifeng://skill/<id>`` 资源）

不支持（M5+）：
    - HTTP / WebSocket transport（仅 stdio）
    - prompts/list, prompts/get
    - sampling/create_message（server → client 反向 LLM 调用）
    - OAuth / authentication
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from taifeng.loop.pool import EnginePool

logger = logging.getLogger(__name__)

# MCP 协议版本（与 stdio_client.py 对齐）
MCP_PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC 2.0 错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# 资源 URI scheme
SKILL_URI_PREFIX = "taifeng://skill/"

# tools/call 内部等 turn 完成的硬上限（防止 mock 永不返回时挂死）
_TURN_WAIT_TIMEOUT_SECONDS = 600.0


class McpServerInitiatedRequestError(Exception):
    """client 对 server-initiated request 回了 JSON-RPC error。"""

    def __init__(
        self, *, code: int, message: str, data: Any = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class McpStdioServer:
    """暴露 ``EnginePool`` 为 MCP server（stdio JSON-RPC 2.0）。

    生命周期：

        pool = await EnginePool.create(...)
        server = McpStdioServer(pool)
        await server.run()  # 阻塞读 stdin 直到 EOF

    业务侧自定义入口示例（CLI 不够用时）：::

        async def main():
            pool = await EnginePool.create(
                skills_dir="...",
                storage_dir="...",
                model_client=MyCustomClient(...),
                hooks=my_hook_runner,
                instruction_layers=[...],
            )
            await McpStdioServer(pool, server_name="my-agent").run()
    """

    def __init__(
        self,
        pool: EnginePool,
        *,
        server_name: str = "taifeng",
        server_version: str | None = None,
        emit: Any = None,
    ) -> None:
        """
        Args:
            pool: 业务侧已配置好的 EnginePool；server **不**自构造
            server_name: 在 MCP initialize handshake 中暴露的 server 名
            server_version: 显式版本号；``None`` 时取 ``taifeng.__version__``
            emit: 可选 async callable ``(event_kind: str, data: dict) -> None``，
                用于 G1 elicitation 生命周期 telemetry（elicitation_started /
                elicitation_completed / elicitation_timed_out）。``None`` 时走
                logger.info（不 raise）。
        """
        from taifeng import __version__

        self._pool = pool
        self._server_name = server_name
        self._server_version = server_version or __version__
        self._emit_cb = emit

        # G1 mcp-server-hitl-elicitation T2: 双向 JSON-RPC 状态
        self._write_lock = asyncio.Lock()
        """所有写 stdout 必须过此锁，防止并发 response 与 outgoing request 写交错。"""

        self._pending_outgoing: dict[str, asyncio.Future[dict[str, Any]]] = {}
        """outgoing request id → future（等 client response）。"""

        self._outgoing_id_counter: int = 0
        """outgoing request id 序号；id 格式 ``srv_<N>``，与 client id 隔离。"""

        # 在 run() 中 bind 到实际 stdout，供 server_initiated_request 写出
        self._stdout: asyncio.StreamWriter | None = None

        # tools/call 以 owned task 派发（读循环不被 turn 阻塞，turn 内的
        # elicitation 才能读到 client 应答）；run() 退出时统一收敛
        self._owned_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Public: run loop
    # ------------------------------------------------------------------

    async def run(
        self,
        stdin: asyncio.StreamReader | None = None,
        stdout: asyncio.StreamWriter | None = None,
    ) -> None:
        """主循环：读 stdin 行 → 派发 → 写 stdout 行。

        ``stdin / stdout`` 可注入用于测试；默认走 sys.stdin / sys.stdout
        的 async 包装（通过 ``loop.connect_read_pipe`` / ``connect_write_pipe``）。
        """
        if stdin is None or stdout is None:
            stdin, stdout = await _connect_std_streams()

        # G1 T2: bind stdout 给 server_initiated_request 用
        self._stdout = stdout

        try:
            while True:
                line = await stdin.readline()
                if not line:  # EOF
                    return
                response = await self._handle_line(line)
                if response is not None:
                    await self._write_message(response)
        except asyncio.CancelledError:
            raise
        finally:
            await self._converge_owned_tasks()
            self._stdout = None

    async def _converge_owned_tasks(self) -> None:
        """取消并等待所有在飞 tools/call task（EOF / 取消退出时不留悬空任务）。"""
        tasks = list(self._owned_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

    def _spawn_tools_call(self, req_id: Any, params: dict[str, Any]) -> None:
        """把一次 tools/call 派成 owned task，完成后自行写回响应。"""
        task = asyncio.create_task(self._run_tools_call(req_id, params))
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)

    async def _run_tools_call(self, req_id: Any, params: dict[str, Any]) -> None:
        """owned task 主体：跑 tools/call，异常兜底为 JSON-RPC internal error 响应。

        取消（run() 收敛）原样上抛、不写响应——stdout 此时可能已失效。
        """
        try:
            response = await self._handle_tools_call(req_id, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 不让单次 turn 的异常打穿读循环
            logger.exception("tools/call failed: id=%s", req_id)
            response = _jsonrpc_error(
                req_id, JSONRPC_INTERNAL_ERROR, f"Internal error: {e}",
            )
        await self._write_message(response)

    # ------------------------------------------------------------------
    # Internal: line / request dispatch
    # ------------------------------------------------------------------

    async def _handle_line(self, raw: bytes) -> dict[str, Any] | None:
        """解析单条 JSON-RPC 行；返回 response dict（notification 返回 None）。

        三种消息形态（G1 T2 改造）：
            1. ``{"id": x, "method": m, ...}``  → incoming request → ``_dispatch``
            2. ``{"id": x, "result"|"error": ..., 无 method}`` → incoming response
               → resolve ``_pending_outgoing[x]``（不走 _dispatch）
            3. ``{"method": m, ...}`` 无 id → notification（既有路径）
        """
        try:
            text = raw.decode("utf-8").strip()
            if not text:
                return None
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return _jsonrpc_error(
                None, JSONRPC_PARSE_ERROR, f"Parse error: {e}"
            )

        if not isinstance(payload, dict):
            return _jsonrpc_error(
                None, JSONRPC_INVALID_REQUEST,
                "Invalid Request: expected JSON object",
            )

        req_id = payload.get("id")
        method = payload.get("method")

        # 路径 2：incoming response（result / error 字段存在，且无 method）
        # 关键判定：method 不是字符串 + (含 result 或 error 字段) → response
        if not isinstance(method, str) and (
            "result" in payload or "error" in payload
        ):
            self._handle_incoming_response(req_id, payload)
            return None

        # 路径 1 / 3：incoming request 或 notification（method 必须是字符串）
        if not isinstance(method, str):
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_REQUEST,
                "Invalid Request: missing method",
            )

        return await self._dispatch(req_id, method, payload.get("params") or {})

    # ------------------------------------------------------------------
    # G1 T2: outgoing request / response 处理
    # ------------------------------------------------------------------

    def _handle_incoming_response(
        self, req_id: Any, payload: dict[str, Any],
    ) -> None:
        """处理 client 对 server-initiated request 的 response。

        命中 ``_pending_outgoing[req_id]`` 时 resolve future；孤儿 response 仅
        log warning，不崩主循环。
        """
        future = self._pending_outgoing.pop(req_id, None)
        if future is None:
            preview = str(payload)[:100]
            logger.warning(
                "orphan response for unknown outgoing id=%r: %s",
                req_id, preview,
            )
            return
        if future.done():
            # 例如已被 cancel；忽略
            return
        if "error" in payload:
            err = payload["error"]
            future.set_exception(
                McpServerInitiatedRequestError(
                    code=err.get("code", -1),
                    message=err.get("message", "unknown_error"),
                    data=err.get("data"),
                )
            )
        else:
            future.set_result(payload.get("result") or {})

    async def _write_message(self, payload: dict[str, Any]) -> None:
        """写 JSON 一行到 stdout，统一过 _write_lock。"""
        if self._stdout is None:
            raise RuntimeError(
                "_write_message called before run() bound stdout"
            )
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._stdout.write(line)
            await self._stdout.drain()

    async def server_initiated_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,
        cancel: Any = None,
    ) -> dict[str, Any]:
        """从 server 主动发起一次 client-bound JSON-RPC 请求并等响应。

        Args:
            method: MCP 方法名（如 ``"elicitation/create"``）
            params: 方法参数
            timeout: 等响应秒数；超时抛 ``TimeoutError`` 且清理 pending future
            cancel: 可选 ``CancellationToken``；触发时 future cancel + 清理 id

        Returns:
            client response 的 result dict

        Raises:
            TimeoutError: 等待超时
            McpServerInitiatedRequestError: client 回了 JSON-RPC error
            RuntimeError: server 尚未启动（_stdout 未 bind）
            asyncio.CancelledError: cancel token 触发
        """
        if self._stdout is None:
            raise RuntimeError(
                "server_initiated_request called before run() started"
            )
        self._outgoing_id_counter += 1
        req_id = f"srv_{self._outgoing_id_counter}"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_outgoing[req_id] = future

        # 注册 cancel token 回调（可选）
        cancel_unsub = None
        if cancel is not None:
            def _on_cancel() -> None:
                if not future.done():
                    future.cancel()
            try:
                cancel_unsub = cancel.add_callback(_on_cancel)
            except AttributeError:
                # 兼容简化 cancel 对象（无 add_callback）
                cancel_unsub = None

        # Telemetry: started
        await self._emit_event("elicitation_started", {
            "method": method,
            "id": req_id,
            "params_preview": (json.dumps(params, ensure_ascii=False)[:200]),
        })

        import time
        t0 = time.monotonic()
        outcome = "ok"
        try:
            await self._write_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            })
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                outcome = "timeout"
                await self._emit_event("elicitation_timed_out", {
                    "method": method,
                    "id": req_id,
                    "timeout": timeout,
                })
                raise
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except McpServerInitiatedRequestError:
                outcome = "error"
                raise
        finally:
            # 清理 pending future（防止迟到 response 触发崩溃）
            self._pending_outgoing.pop(req_id, None)
            if cancel_unsub is not None:
                try:
                    cancel_unsub()
                except Exception:
                    pass
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._emit_event("elicitation_completed", {
                "method": method,
                "id": req_id,
                "duration_ms": duration_ms,
                "outcome": outcome,
            })

    async def _emit_event(self, kind: str, data: dict[str, Any]) -> None:
        """转发到业务侧 emit_cb；为 None 时走 logger.info。"""
        if self._emit_cb is None:
            logger.info("[mcp_server] %s %s", kind, data)
            return
        try:
            await self._emit_cb(kind, data)
        except Exception:
            logger.exception("emit callback failed for kind=%s", kind)

    async def _dispatch(
        self, req_id: Any, method: str, params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按 method 路由到 handler。"""
        if method == "initialize":
            result = self._handle_initialize(params)
        elif method == "tools/list":
            result = self._handle_tools_list(params)
        elif method == "tools/call":
            # 不在读循环内 await 整个 turn：turn 内 McpPrompter 的 elicitation 要靠
            # 读循环继续读 stdin 才能拿到 client 应答
            self._spawn_tools_call(req_id, params)
            return None
        elif method == "resources/list":
            result = self._handle_resources_list(params)
        elif method == "resources/read":
            return self._handle_resources_read(req_id, params)
        elif method.startswith("notifications/"):
            # JSON-RPC notification —— 不需要响应
            return None
        else:
            return _jsonrpc_error(
                req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}",
            )

        # notification 无需响应（id 为 None）
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """initialize handshake —— 协议版本协商 + server info 公告。"""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {
                "name": self._server_name,
                "version": self._server_version,
            },
        }

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """暴露单个 meta-tool ``run_skill_turn``。

        不暴露所有 skill 作为独立 tool —— call_skill 派发受 dispatch_policy 约束需要
        entry skill 上下文，过早暴露会破坏 R1。
        """
        return {
            "tools": [
                {
                    "name": "run_skill_turn",
                    "description": (
                        "Run a taifeng skill for one turn and return the final "
                        "assistant text. The skill must have entry=true; "
                        "sub-skills are invoked via the engine's internal "
                        "call_skill dispatch loop."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "skill_id": {
                                "type": "string",
                                "description": (
                                    "Entry skill id (must have entry=true in "
                                    "its SKILL.md frontmatter)."
                                ),
                            },
                            "message": {
                                "type": "string",
                                "description": "User message text",
                            },
                            "session_id": {
                                "type": "string",
                                "description": (
                                    "Optional engine session key for "
                                    "conversation continuity. Defaults to "
                                    "'mcp-default'."
                                ),
                            },
                        },
                        "required": ["skill_id", "message"],
                        "additionalProperties": False,
                    },
                }
            ]
        }

    async def _handle_tools_call(
        self, req_id: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        """tools/call: 当前仅识别 ``run_skill_turn``。"""
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "run_skill_turn":
            return _jsonrpc_error(
                req_id, JSONRPC_METHOD_NOT_FOUND,
                f"Unknown tool: {name}",
            )

        skill_id = args.get("skill_id")
        message = args.get("message")
        session_id = args.get("session_id") or "mcp-default"

        # 参数缺失 → JSON-RPC 协议级错误（客户端的 bug，而非 LLM 视图）
        if not isinstance(skill_id, str) or not skill_id:
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "Invalid params: 'skill_id' must be a non-empty string",
            )
        if not isinstance(message, str):
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "Invalid params: 'message' must be a string",
            )

        # 业务错误（unknown skill / non-entry）→ MCP 标准 isError content，让客户端
        # LLM 看到原因并自适应（与 JSON-RPC 协议错误区分）
        from taifeng.loop.submission import UserMessage

        try:
            engine = await self._pool.get_or_create(
                session_id=session_id, entry_skill_id=skill_id,
            )
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"skill_unavailable: {e}"}
                    ],
                    "isError": True,
                },
            }
        except RuntimeError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"pool_error: {e}"}
                    ],
                    "isError": True,
                },
            }

        sub_id = await engine.submit(UserMessage(text=message))
        assistant_text = ""
        is_error = False
        try:
            async def _consume() -> None:
                nonlocal assistant_text, is_error
                async for ev in engine.subscribe(sub_id):
                    kind = ev.msg.kind
                    if kind == "assistant_text":
                        assistant_text += ev.msg.data.get("delta", "")
                    elif kind == "turn_completed":
                        return
                    elif kind == "turn_suspended":
                        # turn_suspended 是终结态(turn 挂起等待 Resume)——必须返回，否则会
                        # 空等到 _TURN_WAIT_TIMEOUT_SECONDS 超时。挂起不是错误，isError 不置真；
                        # 在 content 里追加挂起说明 + record_id，供调用方据此提交 Resume。
                        record_id = ev.msg.data.get("record_id")
                        note = (
                            f"\n\n[mcp_server: turn suspended, awaiting input; "
                            f"record_id={record_id}]"
                        )
                        assistant_text = assistant_text + note
                        return
                    elif kind == "turn_failed":
                        is_error = True
                        # turn_failed 时若无 assistant_text，把 error 信息塞 content
                        if not assistant_text:
                            err = ev.msg.data.get("error", "turn_failed")
                            assistant_text = f"turn_failed: {err}"
                        return

            await asyncio.wait_for(_consume(), timeout=_TURN_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            is_error = True
            assistant_text = (
                assistant_text
                + f"\n\n[mcp_server: turn timed out after "
                f"{_TURN_WAIT_TIMEOUT_SECONDS}s]"
            )

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": assistant_text}],
                "isError": is_error,
            },
        }

    def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """暴露所有 skill 作为 MCP 资源（uri = ``taifeng://skill/<id>``）。"""
        snapshot = self._pool.skill_registry.snapshot()
        resources: list[dict[str, Any]] = []
        for skill_id in sorted(snapshot.ids()):
            defn = snapshot.get(skill_id)
            if defn is None:
                continue
            resources.append({
                "uri": f"{SKILL_URI_PREFIX}{skill_id}",
                "name": defn.name,
                "description": defn.description,
                "mimeType": "text/markdown",
            })
        return {"resources": resources}

    def _handle_resources_read(
        self, req_id: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        """resources/read: 解析 ``taifeng://skill/<id>`` 返回 SKILL.md body。"""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith(SKILL_URI_PREFIX):
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                f"Invalid params: unsupported uri scheme {uri!r}",
            )
        skill_id = uri[len(SKILL_URI_PREFIX):]
        defn = self._pool.skill_registry.snapshot().get(skill_id)
        if defn is None:
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                f"Invalid params: unknown skill {skill_id!r}",
            )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": [{
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": defn.body,
                }],
            },
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _jsonrpc_error(
    req_id: Any, code: int, message: str,
) -> dict[str, Any]:
    """构造 JSON-RPC 2.0 error response。"""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


async def _connect_std_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """把 sys.stdin / sys.stdout 包成 asyncio StreamReader / StreamWriter。"""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout,
    )
    writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)
    return reader, writer
